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
from collections.abc import Generator
import contextlib
import fcntl
import json
from dataclasses import dataclass
import os
import select
import selectors
import signal
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import TextIO, TypedDict, cast
import uuid

from scripts.build_hold import override_guard
from scripts.build_hold.owning_session import NoOwningSession, OwningSession, owning_session
from scripts.build_queue import admission_wire as wire
from scripts.lib import conf, process_group
from scripts.lib.atomic_file import write_text_atomic
from scripts.mac_test import claim_state, mac_test
from scripts.mac_test.mac_transport import (BATCH_MODE, MacPlacement, RemoteRun, RemoteState, placement_value,
                                            remote_command, stop_command, stop_remote, stream_remote)


def admission_command() -> list[str]:
    return [os.path.expanduser(word) for word in shlex.split(conf.read("mac_gate").text("admission_client_command"))]


def parse_placement(output: str) -> MacPlacement:
    for line in reversed(output.splitlines()):
        fields = line.split("\t")
        if len(fields) == 6:
            return MacPlacement(fields[2], fields[4], fields[5])
    raise ValueError("the queue returned no placement")


def placement_arguments(episode: str, since: float, pid: int, worktree: Path, package: str,
                        kind: str, source_key: str, session: str, assignment_file: Path,
                        argv: list[str], *, wait_events: bool) -> list[str]:
    return ["place-step", "--once", *(("--wait-events",) if wait_events else ()),
        "--episode", episode, "--since", str(since),
        "--pid", str(pid), "--repo-root", str(worktree), "--package", package, "--kind", kind,
        "--source-key", source_key, "--session-id", session, "--target", "mac",
        "--assignment-file", str(assignment_file), "--token", "none", "--", *argv]


@dataclass(frozen=True)
class NoMacPlacementRefusal:
    pass


@dataclass(frozen=True)
class MacPlacementRefusal:
    reason: str
    has_provider_diagnostic: bool


def mac_placement_refusal(output: str) -> MacPlacementRefusal | NoMacPlacementRefusal:
    reply = wire.parse_reply(output)
    if (isinstance(reply, wire.PlacementWait)
            and isinstance(reply.wait, wire.PlacementResourceWait)
            and reply.wait.resource == "mac" and reply.wait.reason in ("busy", "blocked")):
        return MacPlacementRefusal(reply.wait.reason, any(item.target == "mac" for item in reply.provider_diagnostics))
    return NoMacPlacementRefusal()


def capture_command(command: list[str], *, capture_stderr: bool) -> tuple[int, str, str]:
    with subprocess.Popen(command, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE if capture_stderr else None, text=True) as process:
        try:
            output, errors = process.communicate()
            return process.returncode, output, errors or ""
        finally:
            stop_command(process)


def run_callback(script: str, phase: str, argv_file: Path, arguments: list[str],
                 environment: dict[str, str], *, interrupt_prepare: bool = False) -> int:
    with subprocess.Popen([script, phase, str(argv_file), *arguments], env=environment, text=True) as process:
        try:
            return process.wait()
        except KeyboardInterrupt:
            if interrupt_prepare and process.poll() is None:
                # Let prepare stop its commands and remove its temporary refs.
                process.send_signal(signal.SIGINT)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    _ = process.wait(timeout=conf.read("mac_gate").number("terminate_grace_seconds"))
            raise
        finally:
            stop_command(process)


MAC_BUSY_STATUS = 78
MAC_RUN_REFUSED_STATUS = 9
RUNS = ".local/state/mac-queue/runs"
RECEIPTS = ".local/state/mac-queue/receipts"
MAC_GATE_SCRIPT = Path("scripts/mac_test/mac_gate.py")
MAC_RUN_SCRIPT = Path("scripts/production/mac_run.sh")


class SavedRun(TypedDict):
    lifecycle_script: str
    arguments: list[str]
    host: str


@contextlib.contextmanager
def run_directory(episode: str) -> Generator[Path]:
    """Hold this run's state folder. Its lock ends with the process, so a later run finds a run that a signal ended."""
    runs = Path.home() / RUNS
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


def clean_ended_runs(environment: dict[str, str]) -> None:
    """Finish the cleanup of each run that ended before its own, such as by a signal."""
    runs = Path.home() / RUNS
    for directory in sorted(runs.iterdir()) if runs.is_dir() else []:
        if directory.name.endswith(".new"):
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
            if record.exists():
                saved = cast(SavedRun, json.loads(record.read_text()))
                try:
                    script, phase = ended_run_callback(saved["lifecycle_script"])
                    argv_file = directory / "argv.json"
                    with lifecycle_mirror_lease(argv_file, saved["host"]):
                        status = run_callback(str(script), phase, argv_file, saved["arguments"],
                                              {**environment, "MAC_QUEUE_HOST": saved["host"]})
                except OSError as error:
                    print(f"mac queue: cleanup of an ended run failed: {error}; kept {directory}", file=sys.stderr)
                    continue
                if status:
                    print(f"mac queue: cleanup of an ended run failed ({status}); kept {directory}", file=sys.stderr)
                    continue
            (Path.home() / RECEIPTS / (directory.name + ".json")).unlink(missing_ok=True)
            shutil.rmtree(directory, ignore_errors=True)
        finally:
            os.close(descriptor)


def ended_run_callback(saved_script: str) -> tuple[Path, str]:
    """Select an installed callback for a known script name."""
    installed = Path.home() / ".claude"
    name = Path(saved_script).name
    if name == MAC_GATE_SCRIPT.name:
        return installed / MAC_GATE_SCRIPT, "cleanup-ended"
    if name == MAC_RUN_SCRIPT.name:
        return installed / MAC_RUN_SCRIPT, "cleanup"
    return Path(saved_script), "cleanup"


@contextlib.contextmanager
def lifecycle_mirror_lease(argv_file: Path, host: str) -> Generator[None]:
    """Keep the prepared mirror under the shared host lease through cleanup."""
    try:
        path = Path(argv_file.with_suffix(".json.lease").read_text().strip())
    except FileNotFoundError:
        yield
        return

    from scripts.copies import copies

    with contextlib.ExitStack() as lease:
        try:
            lease.enter_context(copies.remote_mirror_lease(host, path))
        except copies.CopyError as error:
            raise OSError(str(error)) from error
        yield


class Arguments(argparse.Namespace):
    what: str = ""
    kind: str = ""
    package: str = ""
    worktree: Path = Path()
    cwd: str = ""
    lifecycle_script: str = ""
    caller_fd: int = -1
    no_wait: bool = False
    owned_close: bool = False
    argv: list[str] = []

    @property
    def is_mac_run(self) -> bool:
        return self.kind == "run" and Path(self.lifecycle_script).name == MAC_RUN_SCRIPT.name


@dataclass(frozen=True)
class UnreadableMacBlock:
    """The block record cannot be read, so the run waits until it reads again."""

    path: Path


MacRunBlock = claim_state.PendingMacBlock | claim_state.ActiveMacBlock | UnreadableMacBlock


def blocking_run(
    args: Arguments, owner: OwningSession | NoOwningSession,
) -> claim_state.NoMacBlock | MacRunBlock | mac_test.ClaimDenied:
    """Check the Mac block for every run kind: shot, install, probe and run."""
    paths = claim_state.state_paths()
    try:
        block = mac_test.blocking_claim_block(paths, owner)
    except (OSError, claim_state.StateReadError):
        if args.is_mac_run:
            return mac_test.unreadable_claim(paths.block)
        return UnreadableMacBlock(paths.block)
    if not args.is_mac_run:
        return block
    if not isinstance(block, claim_state.NoMacBlock):
        return mac_test.blocked_claim(block)
    try:
        _ = mac_test.read_claims(paths)
    except mac_test.ClaimStateUnreadable as error:
        return mac_test.unreadable_claim(error.path)
    return block


def wait_to_retry() -> None:
    time.sleep(conf.read("build_admission").number("waiting_pass_seconds"))


def shell_session() -> OwningSession | NoOwningSession:
    session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    return OwningSession(session) if session else NoOwningSession()


def smoke_session(placement: MacPlacement, command: list[str], ssh: contextlib.ExitStack) -> subprocess.Popen[str]:
    """Run the numbered copy with the SSH input open."""
    work = f"MAC_PLACEMENT={shlex.quote(placement_value(placement))} {shlex.join(command)}"
    process = ssh.enter_context(subprocess.Popen(
        ["ssh", *BATCH_MODE, placement.host, "/bin/zsh -f -c " + shlex.quote(work)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"))
    _ = ssh.callback(stop_command, process)
    return process


@dataclass(frozen=True)
class SmokeNotReady:
    pass


@dataclass(frozen=True)
class SmokeConnectionLost:
    pass


@dataclass(frozen=True)
class SmokeFinalStatus:
    status: int


@dataclass(frozen=True)
class SmokeMissingStatus:
    pass


@dataclass(frozen=True)
class SmokeInvalidStatus:
    pass


@dataclass(frozen=True)
class SmokeStatusReadTimedOut:
    seconds: float


SmokeResult = (SmokeNotReady | SmokeConnectionLost | SmokeFinalStatus | SmokeMissingStatus
               | SmokeInvalidStatus | SmokeStatusReadTimedOut)


def smoke_status_path(episode: str) -> Path:
    """Name the file inside this smoke run's folder on the Mac."""
    if uuid.UUID(hex=episode).hex != episode:
        raise ValueError("the Mac smoke run has an invalid episode")
    return Path(".local/state/mac-queue/smoke") / episode / "status"


def read_smoke_ready(output: TextIO, episode: str) -> bool:
    ready = f"mac_smoke_ready={episode}\n"
    for line in output:
        if line == ready:
            return True
        print(line, end="", flush=True)
    return False


def read_smoke_result(placement: MacPlacement, ssh_status: int, *, ready: bool) -> SmokeResult:
    """Read and remove the status file after the first SSH command ends."""
    if not ready:
        return SmokeNotReady()
    if ssh_status:
        return SmokeConnectionLost()
    path = shlex.quote(str(smoke_status_path(placement.episode)))
    command = (
        f'status_file="$HOME"/{path}\n'
        '[[ -f $status_file ]] || exit 1\n'
        'builtin printf "mac_smoke_status="\n'
        'cat -- "$status_file" && rm -f -- "$status_file" && rmdir -- "${status_file:h}"'
    )
    timeout = conf.read("mac_test").number("probe_timeout_s")
    try:
        result = subprocess.run(
            ["ssh", *BATCH_MODE, placement.host, "/bin/zsh -f -c " + shlex.quote(command)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return SmokeStatusReadTimedOut(timeout)
    if result.returncode or not result.stdout:
        return SmokeMissingStatus()
    prefix = b"mac_smoke_status="
    if result.stdout.startswith(prefix) and result.stdout.endswith(b"\n"):
        status = result.stdout[len(prefix):-1]
        if 1 <= len(status) <= 3 and status.isdigit() and int(status) <= 255:
            return SmokeFinalStatus(int(status))
    return SmokeInvalidStatus()


def run_smoke_copy(command: list[str]) -> int:
    """Publish the copy command's exit code after the lease closes."""
    episode = os.environ["MAC_PLACEMENT"].partition("/")[0]
    status_file = Path.home() / smoke_status_path(episode)
    status_file.unlink(missing_ok=True)
    result = subprocess.run(command)
    status = result.returncode if result.returncode >= 0 else 128 - result.returncode
    write_text_atomic(status_file, f"{status}\n", new_file_mode=0o600)
    return 0


def run_smoke_work(work: str, cleanup: str) -> int:
    """Stop the smoke work on SSH EOF and settle the copy before its lease ends."""
    from scripts.lib.build_folders import COPY_KEEP_ENV

    episode = os.environ["MAC_PLACEMENT"].partition("/")[0]
    home = Path.home()
    incoming = sys.stdin.fileno()
    if select.select([incoming], [], [], 0)[0]:
        return 1
    environment = dict(os.environ)
    checkout = Path.cwd()
    keep_file = Path(os.environ[COPY_KEEP_ENV])
    _ = keep_file.write_text("Mac smoke checkout clearance is unconfirmed.\n")
    descriptor, token = tempfile.mkstemp(prefix=".mac-smoke-launch-", dir=checkout)
    os.close(descriptor)
    environment["MAC_SMOKE_LAUNCH_TOKEN"] = token
    os.chdir(home)
    locks = tuple(int(number) for number in os.environ.get("COPIES_COMMAND_LEASE_FDS", "").split(",") if number)
    group = subprocess.Popen(["/bin/zsh", "-c", work], cwd=checkout, start_new_session=True,
                             env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, pass_fds=locks)
    assert group.stdout is not None
    output = group.stdout.fileno()
    disconnected = False
    try:
        while True:
            ready, _, _ = select.select([incoming, output], [], [])
            if incoming in ready:
                disconnected = True
                break
            chunk = os.read(output, 65536)
            if not chunk:
                _ = group.wait()
                break
            _ = sys.stdout.buffer.write(chunk)
            _ = sys.stdout.buffer.flush()
    finally:
        if group.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process_group.send_process_group(group.pid, signal.SIGKILL)
            _ = group.wait()
        _ = group.stdout.close()
        cleared = subprocess.run(["/bin/zsh", "-c", cleanup, "cleanup-copy", str(checkout)], cwd=home,
                                 env=environment, stdin=subprocess.DEVNULL)
    if disconnected:
        return 1
    status = group.wait()
    if status < 0:
        status = 128 - status
    status = status or (cleared.returncode if cleared.returncode >= 0 else 128 - cleared.returncode)
    print(f"\nmac_smoke_ready={episode}", flush=True)
    return status if sys.stdin.buffer.read(1) == b"\n" else status or 1


def run(args: Arguments) -> int:
    since = time.time()
    # A block admits its holding unit's workers, so each kind checks the block as the unit's session.
    block_owner = owning_session()
    if isinstance(block_owner, NoOwningSession):
        block_owner = shell_session()
    placement_owner = block_owner if args.kind == "run" else shell_session()
    announced = False
    reported = ""
    try:
        while True:
            block = blocking_run(args, block_owner)
            if isinstance(block, claim_state.NoMacBlock):
                if not announced:
                    print(f"Mac queue: {args.what}", file=sys.stderr)
                    announced = True
                result = run_attempt(args, since, placement_owner, block_owner)
                if isinstance(result, int):
                    return result
                block = result
            if isinstance(block, mac_test.ClaimDenied):
                print(block.reason)
                return MAC_RUN_REFUSED_STATUS
            if isinstance(block, UnreadableMacBlock):
                message = ("Mac block record unreadable" if args.no_wait
                           else "Waiting for a readable Mac block record") + f": {block.path}"
            else:
                block = mac_test.lift_expired_claim_block(claim_state.state_paths(), block_owner, block)
                if isinstance(block, claim_state.NoMacBlock):
                    continue
                holder = mac_test.holder_name(block.holder)
                reason = " ".join(block.reason.splitlines())
                message = ("Mac blocked" if args.no_wait else "Waiting for the Mac block") + f" held by {holder}: {reason}"
            if message != reported:
                print(message, flush=True)
                reported = message
            if args.no_wait:
                return MAC_BUSY_STATUS
            wait_to_retry()
    except (OSError, ValueError) as error:
        print(f"mac queue: {error}", file=sys.stderr)
        return 2


def run_attempt(
    args: Arguments, since: float, placement_owner: OwningSession | NoOwningSession,
    block_owner: OwningSession | NoOwningSession,
) -> int | MacRunBlock:
    episode, pid = uuid.uuid4().hex, os.getpid()
    source = subprocess.run(["git", "-C", str(args.worktree), "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    source_key = source.stdout.strip() if source.returncode == 0 else ""
    command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    original_command = command.copy()
    status = 1
    block: claim_state.NoMacBlock | MacRunBlock | mac_test.ClaimDenied = mac_test.NO_MAC_BLOCK
    remote = RemoteRun()
    placement = MacPlacement("", episode, "")
    receipts = Path.home() / RECEIPTS
    receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
    assignment_file = receipts / (episode + ".json")
    # place-step reads `what` from this file, so the Builds row shows `<owner>: <what>`.
    _ = assignment_file.write_text(json.dumps({"episode": episode, "pid": pid, "what": args.what}))
    assignment_file.chmod(0o600)
    with run_directory(episode) as directory, contextlib.ExitStack() as mirror, contextlib.ExitStack() as ssh:
        argv_file = directory / "argv.json"
        callback_environment = dict(os.environ)
        cleanup_leased = False
        mac_run = args.is_mac_run
        smoke: subprocess.Popen[str] | None = None
        smoke_ready = False
        cleaned = False
        try:
            while True:
                code, output, errors = capture_command(admission_command() + placement_arguments(episode, since, pid,
                    args.worktree, args.package, args.kind, source_key, claim_state.session_id(placement_owner),
                    assignment_file, command, wait_events=not mac_run), capture_stderr=mac_run)
                if code in (0, MAC_BUSY_STATUS):
                    block = blocking_run(args, block_owner)
                if errors and (code != MAC_BUSY_STATUS or isinstance(block, claim_state.NoMacBlock)):
                    print(errors, end="", file=sys.stderr, flush=True)
                refusal = (mac_placement_refusal(output)
                           if code == MAC_BUSY_STATUS and mac_run and isinstance(block, claim_state.NoMacBlock)
                           else NoMacPlacementRefusal())
                if (code != MAC_BUSY_STATUS or args.no_wait or isinstance(refusal, MacPlacementRefusal)
                        or not isinstance(block, claim_state.NoMacBlock)):
                    break
                if not mac_run:
                    print(output, end="", flush=True)
                wait_to_retry()
            if not isinstance(block, claim_state.NoMacBlock):
                status = 0
            elif isinstance(refusal, MacPlacementRefusal):
                if not errors or not refusal.has_provider_diagnostic:
                    print(f"Mac is {refusal.reason}", file=sys.stderr, flush=True)
                status = MAC_RUN_REFUSED_STATUS
            elif code:
                if code != MAC_BUSY_STATUS or not mac_run:
                    print(output, end="", flush=True)
                status = MAC_RUN_REFUSED_STATUS if args.lifecycle_script and code in (10, 11, 12) else code
            else:
                placement = parse_placement(output)
                callback_environment["MAC_QUEUE_HOST"] = placement.host
                clean_ended_runs(callback_environment)
                block = blocking_run(args, block_owner)
                on_close = ""
                if isinstance(block, claim_state.NoMacBlock) and args.lifecycle_script:
                    saved: SavedRun = {"lifecycle_script": args.lifecycle_script, "arguments": original_command,
                                       "host": placement.host}
                    write_text_atomic(directory / "run.json", json.dumps(saved))
                    try:
                        status = run_callback(args.lifecycle_script, "prepare", argv_file, original_command,
                                              callback_environment, interrupt_prepare=args.owned_close)
                    finally:
                        interrupted = isinstance(sys.exception(), KeyboardInterrupt)
                        try:
                            mirror.enter_context(lifecycle_mirror_lease(argv_file, placement.host))
                        except (OSError, ValueError) as error:
                            if not interrupted:
                                raise
                            print(f"mac queue: lifecycle cleanup lease unavailable: {error}", file=sys.stderr)
                        else:
                            cleanup_leased = True
                    if status == 0:
                        command = cast(list[str], json.loads(argv_file.read_text()))
                        close_path = argv_file.with_suffix(".json.close")
                        on_close = close_path.read_text() if close_path.exists() else ""
                else:
                    status = 0
                if status == 0 and isinstance(block, claim_state.NoMacBlock):
                    block = blocking_run(args, block_owner)
                    if isinstance(block, claim_state.NoMacBlock):
                        # Record failure until the Mac reports an exit status.
                        status = 1
                        if args.owned_close and not on_close:
                            raise ValueError("the run needs a close hook")
                        if mac_run:
                            smoke = smoke_session(placement, command, ssh)
                            remote.state = RemoteState.LIVE
                            assert smoke.stdin is not None and smoke.stdout is not None
                            smoke_ready = read_smoke_ready(cast(TextIO, smoke.stdout), placement.episode)
                            if smoke_ready:
                                status = 0
                        else:
                            status = stream_remote(remote_command(placement, args.cwd, command, on_close,
                                                                  owned_close=args.owned_close), remote, ssh)
            if isinstance(block, mac_test.ClaimDenied):
                print(block.reason)
                status = MAC_RUN_REFUSED_STATUS
        except (OSError, ValueError) as error:
            print(f"mac queue: {error}", file=sys.stderr)
            status = 2
        finally:
            safe = remote.state != RemoteState.LIVE or smoke_ready
            if not safe and not args.owned_close and not mac_run:
                ssh.close()
                try:
                    safe = stop_remote(placement, remote)
                except OSError:
                    safe = False
            if cleanup_leased and (safe or args.owned_close):
                try:
                    cleanup_status = run_callback(args.lifecycle_script, "cleanup", argv_file, original_command, callback_environment)
                except OSError as error:
                    print(f"mac queue: lifecycle cleanup failed: {error}", file=sys.stderr)
                    cleanup_status = 2
                if cleanup_status:
                    print(f"mac queue: lifecycle cleanup failed ({cleanup_status})", file=sys.stderr)
                    status = status or cleanup_status
                    if args.owned_close and remote.state is not RemoteState.NOT_STARTED:
                        safe = False
                else:
                    safe = True
                    cleaned = True
            if smoke is not None:
                assert smoke.stdin is not None and smoke.stdout is not None
                try:
                    with contextlib.suppress(BrokenPipeError):
                        if smoke_ready and sys.exception() is None:
                            # Release the copy after local cleanup. EOF alone means the connection ended.
                            _ = smoke.stdin.write("\n")
                            smoke.stdin.flush()
                    with contextlib.suppress(BrokenPipeError):
                        smoke.stdin.close()
                    for line in cast(TextIO, smoke.stdout):
                        print(line, end="", flush=True)
                    smoke_result = read_smoke_result(placement, smoke.wait(), ready=smoke_ready)
                    match smoke_result:
                        case SmokeFinalStatus(remote_status):
                            remote.state = RemoteState.EXITED
                            status = remote_status or status
                            safe = True
                        case _:
                            match smoke_result:
                                case SmokeNotReady():
                                    detail = "the Mac returned no valid final status because the ready line is missing"
                                case SmokeConnectionLost():
                                    detail = "the SSH connection closed before the Mac run ended"
                                case SmokeStatusReadTimedOut(seconds):
                                    detail = f"the Mac status read timed out after {seconds:g} seconds"
                                case SmokeMissingStatus() | SmokeInvalidStatus():
                                    detail = "the Mac returned no valid final status"
                            print(f"mac queue: {detail}", file=sys.stderr)
                            status = 255
                            safe = False
                except (OSError, ValueError) as error:
                    print(f"mac queue: the Mac smoke session did not finish: {error}", file=sys.stderr)
                    status = status or 2
                    safe = False
            if cleaned and safe:
                (directory / "run.json").unlink()
            ssh.close()
            mirror.close()
            # A Mac placement has no local grant, so the run returns it with a cancel that names the result.
            reason = (f"the Mac run ended with status {status}" if remote.state != RemoteState.NOT_STARTED
                      else "the Mac run did not start")
            if safe:
                try:
                    result = subprocess.run(admission_command() + ["cancel-placement", "--episode", episode,
                        "--pid", str(pid), "--reason", reason], stdout=subprocess.DEVNULL)
                    released = result.returncode == 0
                except OSError:
                    released = False
            else:
                released = False
                print("mac queue: the Mac run may still be live; placement release is withheld", file=sys.stderr)
            # The queue ends the placement when this process ends, so the receipt has no later use.
            assignment_file.unlink(missing_ok=True)
            for receipt in receipts.glob(assignment_file.name + ".*"):
                receipt.unlink(missing_ok=True)
            if not released:
                print("mac queue: the placement was not returned; the queue ends it when this run ends", file=sys.stderr)
                status = status or 1
    if status == 0 and not isinstance(block, (claim_state.NoMacBlock, mac_test.ClaimDenied)):
        return block
    return status


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


def watcher_ready(output: int, timeout: float) -> bool:
    expected = b"ready\n"
    received = b""
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as readiness:
        _ = readiness.register(output, selectors.EVENT_READ)
        while received != expected:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not readiness.select(remaining):
                return False
            chunk = os.read(output, len(expected) - len(received))
            received += chunk
            if not chunk or not expected.startswith(received):
                return False
    return True


@contextlib.contextmanager
def caller_watch(caller: int) -> Generator[None]:
    pid = os.getpid()
    if os.getpgrp() != pid:
        raise ValueError("the Mac queue must own its process group")
    queue_thread = threading.get_ident()
    reader, writer = os.pipe()
    closing = threading.Event()
    failed = threading.Event()
    try:
        try:
            watcher = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "watch-caller",
                                        str(caller), str(reader)],
                                       pass_fds=(caller, reader), start_new_session=True,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        finally:
            os.close(reader)
            os.close(caller)
        with watcher:
            try:
                assert watcher.stdout is not None
                config = conf.read("mac_gate")
                startup_timeout = config.whole("placement_check_timeout_seconds")
                grace = config.number("terminate_grace_seconds")
                if not watcher_ready(watcher.stdout.fileno(), startup_timeout):
                    raise ValueError("the Mac queue caller watch did not start")

                def monitor() -> None:
                    status = watcher.wait()
                    if closing.is_set():
                        return
                    if status != CALLER_EXITED:
                        print(f"mac queue: caller watch ended unexpectedly ({status})", file=sys.stderr, flush=True)
                        failed.set()
                    # Wake the queue thread if it is blocked reading SSH output.
                    signal.pthread_kill(queue_thread, signal.SIGINT)

                supervisor = threading.Thread(target=monitor, daemon=True)
                supervisor.start()
                try:
                    yield
                finally:
                    closing.set()
                    os.close(writer)
                    writer = -1
                # Detect a watcher that failed just as queue work finished.
                try:
                    status = watcher.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    status = 2
                if status:
                    if status != CALLER_EXITED:
                        failed.set()
                    raise ValueError(f"the Mac queue caller watch ended with status {status}")
            finally:
                stop_command(watcher)
    finally:
        if writer >= 0:
            os.close(writer)
        if failed.is_set():
            process_group.terminate_owned_process_group()


def main(argv: list[str] | None = None) -> int:
    words = sys.argv[1:] if argv is None else argv
    if len(words) == 3 and words[0] == "watch-caller":
        return watch_caller(int(words[1]), int(words[2]))
    if len(words) == 3 and words[0] == "smoke-work":
        return run_smoke_work(words[1], words[2])
    if len(words) > 1 and words[0] == "smoke-copy":
        return run_smoke_copy(words[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--what", required=True)
    _ = parser.add_argument("--kind", choices=("shot", "install", "probe", "run"), required=True)
    _ = parser.add_argument("--package", required=True)
    _ = parser.add_argument("--worktree", type=Path, default=Path.cwd())
    _ = parser.add_argument("--cwd", default="")
    _ = parser.add_argument("--lifecycle-script", default="")
    _ = parser.add_argument("--caller-fd", type=int, help=argparse.SUPPRESS)
    _ = parser.add_argument("--owned-close", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--no-wait", action="store_true", help="exit with status 78 when the Mac is busy or blocked")
    _ = parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv, namespace=Arguments())
    if not args.argv or args.argv == ["--"]:
        parser.error("an argv is required")
    if args.kind == "run":
        override_guard.refuse_overrides(Path(__file__), "mac_test/mac_queue_run.py")
    if args.caller_fd < 0:
        return run(args)
    try:
        with caller_watch(args.caller_fd):
            return run(args)
    except (OSError, ValueError) as error:
        print(f"mac queue: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

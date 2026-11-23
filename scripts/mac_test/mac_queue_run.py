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
import math
from dataclasses import dataclass
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
from typing import TypedDict, cast
import uuid

from scripts.build_hold import override_guard
from scripts.build_hold.owning_session import NoOwningSession, OwningSession, owning_session
from scripts.build_queue import admission_wire as wire
from scripts.lib import conf, process_group
from scripts.lib.atomic_file import write_text_atomic
from scripts.mac_test import claim_state, mac_test
from scripts.mac_test.caller_watch import CALLER_EXITED
from scripts.mac_test.mac_transport import (MacPlacement, RemoteRun, RemoteState, remote_command, stop_command,
                                            stop_remote, stream_remote)


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


def blocking_run(
    args: Arguments, owner: OwningSession | NoOwningSession,
) -> claim_state.MacBlockState | mac_test.ClaimDenied:
    if args.kind != "run":
        return mac_test.NO_MAC_BLOCK
    paths = claim_state.state_paths()
    try:
        block = mac_test.blocking_claim_block(paths, owner)
    except (OSError, claim_state.StateReadError):
        if args.is_mac_run:
            return mac_test.unreadable_claim(paths.block)
        raise
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


def run(args: Arguments) -> int:
    since = time.time()
    owner = owning_session() if args.kind == "run" else NoOwningSession()
    if isinstance(owner, NoOwningSession):
        session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        if session:
            owner = OwningSession(session)
    announced = False
    reported_block = False
    try:
        while True:
            block = blocking_run(args, owner)
            if isinstance(block, claim_state.NoMacBlock):
                if not announced:
                    print(f"Mac queue: {args.what}", file=sys.stderr)
                    announced = True
                result = run_attempt(args, since, owner)
                if isinstance(result, int):
                    return result
                block = result
            if isinstance(block, mac_test.ClaimDenied):
                print(block.reason)
                return MAC_RUN_REFUSED_STATUS
            block = mac_test.lift_expired_claim_block(claim_state.state_paths(), owner, block)
            if isinstance(block, claim_state.NoMacBlock):
                continue
            if not reported_block:
                holder = mac_test.holder_name(block.holder)
                reason = " ".join(block.reason.splitlines())
                message = "Mac blocked" if args.no_wait else "Waiting for the Mac block"
                print(f"{message} held by {holder}: {reason}", flush=True)
                reported_block = True
            if args.no_wait:
                return MAC_BUSY_STATUS
            wait_to_retry()
    except (OSError, ValueError) as error:
        print(f"mac queue: {error}", file=sys.stderr)
        return 2


def run_attempt(
    args: Arguments, since: float, owner: OwningSession | NoOwningSession,
) -> int | claim_state.PendingMacBlock | claim_state.ActiveMacBlock:
    episode, pid = uuid.uuid4().hex, os.getpid()
    source = subprocess.run(["git", "-C", str(args.worktree), "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    source_key = source.stdout.strip() if source.returncode == 0 else ""
    command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    original_command = command.copy()
    status = 1
    block: claim_state.MacBlockState | mac_test.ClaimDenied = mac_test.NO_MAC_BLOCK
    remote = RemoteRun()
    placement = MacPlacement("", episode, "")
    receipts = Path.home() / RECEIPTS
    receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
    assignment_file = receipts / (episode + ".json")
    _ = assignment_file.write_text(json.dumps({"episode": episode, "pid": pid}))
    assignment_file.chmod(0o600)
    with run_directory(episode) as directory, contextlib.ExitStack() as mirror, contextlib.ExitStack() as ssh:
        argv_file = directory / "argv.json"
        callback_environment = dict(os.environ)
        cleanup_leased = False
        mac_run = args.is_mac_run
        try:
            while True:
                code, output, errors = capture_command(admission_command() + placement_arguments(episode, since, pid,
                    args.worktree, args.package, args.kind, source_key, claim_state.session_id(owner),
                    assignment_file, command, wait_events=not mac_run), capture_stderr=mac_run)
                if code in (0, MAC_BUSY_STATUS):
                    block = blocking_run(args, owner)
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
                block = blocking_run(args, owner)
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
                    block = blocking_run(args, owner)
                    if isinstance(block, claim_state.NoMacBlock):
                        # Record failure until the Mac reports an exit status.
                        status = 1
                        if args.owned_close and not on_close:
                            raise ValueError("the run needs a close hook")
                        status = stream_remote(remote_command(placement, args.cwd, command, on_close,
                                                              owned_close=args.owned_close), remote, ssh)
            if isinstance(block, mac_test.ClaimDenied):
                print(block.reason)
                status = MAC_RUN_REFUSED_STATUS
        except (OSError, ValueError) as error:
            print(f"mac queue: {error}", file=sys.stderr)
            status = 2
        finally:
            if not args.owned_close:
                ssh.close()
            safe = remote.state != RemoteState.LIVE
            if not safe and not args.owned_close:
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
                    (directory / "run.json").unlink()
            # Owned cleanup uses the close hook while SSH still holds its input open.
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
    if status == 0 and isinstance(block, (claim_state.PendingMacBlock, claim_state.ActiveMacBlock)):
        return block
    return status


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
            # -I -S skips site and the user path; the watcher imports only the standard library.
            watch_script = Path(__file__).resolve().with_name("caller_watch.py")
            watcher = subprocess.Popen([sys.executable, "-I", "-S", str(watch_script), str(caller), str(reader)],
                                       pass_fds=(caller, reader), start_new_session=True,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        finally:
            os.close(reader)
            os.close(caller)
        with watcher:
            try:
                assert watcher.stdout is not None
                output = watcher.stdout.fileno()
                readiness = select.poll()
                readiness.register(output, select.POLLIN)
                grace = conf.read("mac_gate").number("terminate_grace_seconds")
                if not readiness.poll(math.ceil(grace * 1000)) or os.read(output, 6) != b"ready\n":
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

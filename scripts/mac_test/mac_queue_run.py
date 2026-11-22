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
from typing import TextIO, TypedDict, cast
import uuid

from scripts.lib import conf, process_group
from scripts.lib.atomic_file import write_text_atomic


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


def stop_remote(placement: MacPlacement, remote: RemoteRun) -> bool:
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
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", placement.host,
                             "/bin/zsh -c " + shlex.quote(script)], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == f"mac_stopped={remote.group}"


def stop_command(process: subprocess.Popen[str] | subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            _ = process.wait(timeout=conf.read("mac_gate").number("terminate_grace_seconds"))
        except subprocess.TimeoutExpired:
            process.kill()
            _ = process.wait()


def capture_command(command: list[str]) -> tuple[int, str]:
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:
        try:
            output, errors = process.communicate()
            del errors
            return process.returncode, output
        finally:
            stop_command(process)


def stream_remote(command: list[str], remote: RemoteRun, ssh: contextlib.ExitStack) -> int:
    process = ssh.enter_context(subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"))
    _ = ssh.callback(stop_command, process)
    remote.state = RemoteState.LIVE
    assert process.stdout is not None
    last = ""
    for line in cast(TextIO, process.stdout):
        if line.startswith("mac_group=") and line.removeprefix("mac_group=").strip().isdigit():
            remote.group = int(line.removeprefix("mac_group="))
            continue
        print(line, end="", flush=True)
        last = line.rstrip("\n")
    ssh_status = process.wait()
    if last.startswith("mac_exit=") and last.removeprefix("mac_exit=").isdigit():
        remote.state = RemoteState.EXITED
        return int(last.removeprefix("mac_exit="))
    print("mac queue: the Mac returned no exit status", file=sys.stderr)
    return ssh_status or 255


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
RUNS = ".local/state/mac-queue/runs"
RECEIPTS = ".local/state/mac-queue/receipts"
MAC_GATE_SCRIPT = Path("scripts/mac_test/mac_gate.py")


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
    if name == "mac_run.sh":
        return installed / "scripts/production/mac_run.sh", "cleanup"
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


def run(args: Arguments) -> int:
    episode, since, pid = uuid.uuid4().hex, time.time(), os.getpid()
    session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    source = subprocess.run(["git", "-C", str(args.worktree), "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    source_key = source.stdout.strip() if source.returncode == 0 else ""
    command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    print(f"Mac queue: {args.what}", file=sys.stderr)
    original_command = command.copy()
    status = 1
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
        try:
            while True:
                code, output = capture_command(admission_command() + placement_arguments(episode, since, pid,
                    args.worktree, args.package, args.kind, source_key, session, assignment_file, command))
                if code != MAC_BUSY_STATUS or args.no_wait:
                    break
                print(output, end="", flush=True)
                time.sleep(conf.read("build_admission").number("waiting_pass_seconds"))
            if code:
                print(output, end="", flush=True)
                status = 9 if args.lifecycle_script and code in (10, 11, 12) else code
            else:
                placement = parse_placement(output)
                callback_environment["MAC_QUEUE_HOST"] = placement.host
                clean_ended_runs(callback_environment)
                on_close = ""
                if args.lifecycle_script:
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
                if status == 0:
                    # A run that ends before the Mac answers reports a failure, not success.
                    status = 1
                    if args.owned_close and not on_close:
                        raise ValueError("the run needs a close hook")
                    status = stream_remote(remote_command(placement, args.cwd, command, on_close,
                                                          owned_close=args.owned_close), remote, ssh)
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
            if not released:
                print("mac queue: the placement was not returned; the queue ends it when this run ends", file=sys.stderr)
                status = status or 1
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
    _ = parser.add_argument("--caller-fd", type=int, help=argparse.SUPPRESS)
    _ = parser.add_argument("--owned-close", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--no-wait", action="store_true", help="exit with status 78 when the Mac is busy")
    _ = parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv, namespace=Arguments())
    if not args.argv or args.argv == ["--"]:
        parser.error("an argv is required")
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

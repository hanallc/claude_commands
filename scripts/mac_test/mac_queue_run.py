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
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from typing import TextIO, TypedDict, cast
import uuid

from scripts.lib import conf


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


def remote_wrapper(work: str, on_close: str, grace_polls: int) -> str:
    """Run the work in the SSH session's process group, and stop the work when the SSH input closes.

    zsh refuses job control without a terminal, so the work shares the session's group. When the
    input closes, the watcher ends every other process of the group, then runs `on_close` (work
    such as a GUI app that started outside the group).
    """
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


def remote_command(placement: MacPlacement, cwd: str, argv: list[str], on_close: str) -> list[str]:
    work = ("cd " + shlex.quote(cwd) + " && " if cwd else "")
    work += "MAC_PLACEMENT=" + shlex.quote(placement.episode + "/" + placement.attempt) + " " + shlex.join(argv)
    # Ten polls a second while the group ends.
    grace_polls = max(1, math.ceil(conf.read("mac_gate").number("terminate_grace_seconds") * 10))
    wrapper = remote_wrapper(work, on_close, grace_polls)
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


def stop_command(process: subprocess.Popen[str]) -> None:
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


def stream_remote(command: list[str], remote: RemoteRun) -> int:
    # The SSH input stays open and empty while this process lives; the Mac wrapper stops the work when it closes.
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
                          errors="replace") as process:
        try:
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
        finally:
            stop_command(process)


def run_callback(script: str, phase: str, argv_file: Path, arguments: list[str], environment: dict[str, str]) -> int:
    with subprocess.Popen([script, phase, str(argv_file), *arguments], env=environment, text=True) as process:
        try:
            return process.wait()
        finally:
            stop_command(process)


RUNS = ".local/state/mac-queue/runs"
RECEIPTS = ".local/state/mac-queue/receipts"


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
                status = run_callback(saved["lifecycle_script"], "cleanup", directory / "argv.json",
                                      saved["arguments"], {**environment, "MAC_QUEUE_HOST": saved["host"]})
                if status:
                    print(f"mac queue: cleanup of an ended run failed ({status}); kept {directory}", file=sys.stderr)
                    continue
            (Path.home() / RECEIPTS / (directory.name + ".json")).unlink(missing_ok=True)
            shutil.rmtree(directory, ignore_errors=True)
        finally:
            os.close(descriptor)


class Arguments(argparse.Namespace):
    what: str = ""
    kind: str = ""
    package: str = ""
    worktree: Path = Path()
    cwd: str = ""
    lifecycle_script: str = ""
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
    prepared = False
    placement = MacPlacement("", episode, "")
    receipts = Path.home() / RECEIPTS
    receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
    assignment_file = receipts / (episode + ".json")
    _ = assignment_file.write_text(json.dumps({"episode": episode, "pid": pid}))
    assignment_file.chmod(0o600)
    with run_directory(episode) as directory:
        argv_file = directory / "argv.json"
        callback_environment = dict(os.environ)
        lease_descriptor = -1
        try:
            while True:
                code, output = capture_command(admission_command() + placement_arguments(episode, since, pid,
                    args.worktree, args.package, args.kind, source_key, session, assignment_file, command))
                if code != 78:
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
                    prepared = True
                    saved: SavedRun = {"lifecycle_script": args.lifecycle_script, "arguments": original_command,
                                       "host": placement.host}
                    _ = (directory / "run.json").write_text(json.dumps(saved))
                    status = run_callback(args.lifecycle_script, "prepare", argv_file, original_command, callback_environment)
                    if status == 0:
                        command = cast(list[str], json.loads(argv_file.read_text()))
                        close_path = argv_file.with_suffix(".json.close")
                        on_close = close_path.read_text() if close_path.exists() else ""
                        lease_path = argv_file.with_suffix(".json.lease")
                        if lease_path.exists():
                            lease_descriptor = os.open(lease_path.read_text().strip(), os.O_WRONLY | os.O_CREAT, 0o600)
                            fcntl.flock(lease_descriptor, fcntl.LOCK_EX)
                else:
                    status = 0
                if status == 0:
                    # A run that ends before the Mac answers reports a failure, not success.
                    status = 1
                    status = stream_remote(remote_command(placement, args.cwd, command, on_close), remote)
        except (OSError, ValueError) as error:
            print(f"mac queue: {error}", file=sys.stderr)
            status = 2
        finally:
            safe = remote.state != RemoteState.LIVE
            if not safe:
                try:
                    safe = stop_remote(placement, remote)
                except OSError:
                    safe = False
            if prepared and safe:
                try:
                    cleanup_status = run_callback(args.lifecycle_script, "cleanup", argv_file, original_command, callback_environment)
                except OSError as error:
                    print(f"mac queue: lifecycle cleanup failed: {error}", file=sys.stderr)
                    cleanup_status = 2
                if cleanup_status:
                    print(f"mac queue: lifecycle cleanup failed ({cleanup_status})", file=sys.stderr)
                    status = status or cleanup_status
            if lease_descriptor >= 0:
                os.close(lease_descriptor)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--what", required=True)
    _ = parser.add_argument("--kind", choices=("shot", "install", "probe", "run"), required=True)
    _ = parser.add_argument("--package", required=True)
    _ = parser.add_argument("--worktree", type=Path, default=Path.cwd())
    _ = parser.add_argument("--cwd", default="")
    _ = parser.add_argument("--lifecycle-script", default="")
    _ = parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv, namespace=Arguments())
    if not args.argv or args.argv == ["--"]:
        parser.error("an argv is required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

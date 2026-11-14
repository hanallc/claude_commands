"""Take a Mac queue placement, run through SSH, and return the placement."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as package_root:
        exec(package_root.read(), {"__file__": package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from types import FrameType
from typing import TextIO, cast
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


def remote_command(placement: MacPlacement, cwd: str, argv: list[str]) -> list[str]:
    work = ("cd " + shlex.quote(cwd) + " && " if cwd else "")
    work += "MAC_PLACEMENT=" + shlex.quote(placement.episode + "/" + placement.attempt) + " " + shlex.join(argv)
    wrapper = "work=" + shlex.quote(work) + r"""
set -m
/bin/zsh -c 'kill -STOP $$; exec /bin/zsh -c "$1"' mac-queue "$work" &
group=$!
while kill -0 "$group" 2>/dev/null; do
    [[ $(ps -p "$group" -o state=) != *T* ]] || break
    sleep 0.01
done
printf 'mac_group=%s\n' "$group"
kill -CONT -- -"$group" 2>/dev/null
wait "$group"
printf '\nmac_exit=%s\n' $?
"""
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
    script = f"""group={remote.group}
kill -TERM -- -"$group" 2>/dev/null
sleep {grace}
kill -KILL -- -"$group" 2>/dev/null
groups=$(ps -axo pgid=) || exit 1
if printf '%s\\n' "$groups" | grep -Eq "^[[:space:]]*$group[[:space:]]*$"; then exit 1; fi
printf 'mac_stopped=%s\\n' "$group"
"""
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", placement.host,
                             "/bin/zsh -c " + shlex.quote(script)], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == f"mac_stopped={remote.group}"


class RunInterrupted(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(f"signal {signum}")
        self.status: int = 128 + signum


def interrupt_run(signum: int, frame: FrameType | None) -> None:
    del frame
    raise RunInterrupted(signum)


def stop_command(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            _ = process.wait(timeout=conf.read("mac_gate").number("terminate_grace_seconds"))
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            _ = process.wait()


def capture_command(command: list[str]) -> tuple[int, str]:
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, start_new_session=True) as process:
        try:
            output, errors = process.communicate()
            del errors
            return process.returncode, output
        finally:
            stop_command(process)


def stream_remote(command: list[str], remote: RemoteRun) -> int:
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                          start_new_session=True) as process:
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
    with subprocess.Popen([script, phase, str(argv_file), *arguments], env=environment, text=True,
                          start_new_session=True) as process:
        try:
            return process.wait()
        finally:
            stop_command(process)


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
    receipts = Path.home() / ".local/state/mac-queue/receipts"
    receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
    assignment_file = receipts / (episode + ".json")
    _ = assignment_file.write_text(json.dumps({"episode": episode, "pid": pid}))
    assignment_file.chmod(0o600)
    with tempfile.TemporaryDirectory(prefix="mac-queue-") as temporary:
        argv_file = Path(temporary) / "argv.json"
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
                if args.lifecycle_script:
                    prepared = True
                    status = run_callback(args.lifecycle_script, "prepare", argv_file, original_command, callback_environment)
                    if status == 0:
                        command = cast(list[str], json.loads(argv_file.read_text()))
                        lease_path = argv_file.with_suffix(".json.lease")
                        if lease_path.exists():
                            lease_descriptor = os.open(lease_path.read_text().strip(), os.O_WRONLY | os.O_CREAT, 0o600)
                            fcntl.flock(lease_descriptor, fcntl.LOCK_EX)
                else:
                    status = 0
                if status == 0:
                    status = stream_remote(remote_command(placement, args.cwd, command), remote)
        except RunInterrupted as error:
            status = error.status
        except (OSError, ValueError) as error:
            print(f"mac queue: {error}", file=sys.stderr)
            status = 2
        finally:
            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                _ = signal.signal(signum, signal.SIG_IGN)
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
            cleanup = (["finish-step", "--assignment-file", str(assignment_file), "--status", str(status)]
                       if remote.state != RemoteState.NOT_STARTED else
                       ["cancel-placement", "--episode", episode, "--pid", str(pid)])
            retry_command = admission_command() + cleanup
            if safe:
                try:
                    result = subprocess.run(retry_command, stdout=subprocess.DEVNULL)
                    released = result.returncode == 0
                except OSError:
                    released = False
            else:
                released = False
                print("mac queue: the Mac run may still be live; placement release is withheld", file=sys.stderr)
            if released:
                assignment_file.unlink(missing_ok=True)
            else:
                print(f"mac queue: receipt kept at {assignment_file}; return with: {shlex.join(retry_command)} && rm -f -- {shlex.quote(str(assignment_file))}",
                      file=sys.stderr)
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
    previous = {signum: signal.signal(signum, interrupt_run)
                for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        return run(args)
    finally:
        for signum, handler in previous.items():
            _ = signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())

"""Run an argv on the Mac over SSH with MAC_PLACEMENT, and stop it when the SSH input closes.

mac_queue_run.py and the hand made Mac route use these functions. The caller takes and returns the
placement. It sets no signal handler.
"""
from __future__ import annotations

from collections.abc import Callable
import contextlib
from dataclasses import dataclass
from enum import Enum
import math
import os
import shlex
import subprocess
import sys
from typing import TextIO, cast

from scripts.lib import conf

# SSH asks no question: a run with no key fails at once.
BATCH_MODE = ("-o", "BatchMode=yes")
# The gate passes a local descriptor for the parsed exit status.
MAC_GATE_EXIT_STATUS_FD = "MAC_GATE_EXIT_STATUS_FD"


@dataclass(frozen=True)
class MacPlacement:
    host: str
    episode: str
    attempt: str


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


def placement_value(placement: MacPlacement) -> str:
    """The value of MAC_PLACEMENT, which the Mac cargo gate checks."""
    return placement.episode + "/" + placement.attempt


def remote_command(placement: MacPlacement, cwd: str, argv: list[str], on_close: str,
                   *, owned_close: bool = False) -> list[str]:
    work = ("cd " + shlex.quote(cwd) + " && " if cwd else "")
    work += "MAC_PLACEMENT=" + shlex.quote(placement_value(placement)) + " " + shlex.join(argv)
    return wrapped_command(placement.host, work, on_close, owned_close=owned_close)


def wrapped_command(host: str, work: str, on_close: str, *, owned_close: bool = False) -> list[str]:
    """The SSH command that runs the shell text work in the remote wrapper."""
    # Ten polls a second while the group ends.
    grace_polls = max(1, math.ceil(conf.read("mac_gate").number("terminate_grace_seconds") * 10))
    wrapper = remote_wrapper(work, on_close, grace_polls, owned_close=owned_close)
    return ["ssh", *BATCH_MODE, host, "/bin/zsh -c " + shlex.quote(wrapper)]


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
    result = subprocess.run(["ssh", *BATCH_MODE, placement.host,
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


def print_line(line: str) -> None:
    print(line, end="", flush=True)


def stream_remote(command: list[str], remote: RemoteRun, ssh: contextlib.ExitStack,
                  *, write: Callable[[str], None] = print_line) -> int:
    """Pass each output line of the Mac work to write, and return the Mac exit status.

    remote.state is EXITED only when the Mac reported the exit status of the work.
    """
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
        write(line)
        last = line.rstrip("\n")
    ssh_status = process.wait()
    if last.startswith("mac_exit=") and last.removeprefix("mac_exit=").isdigit():
        status = int(last.removeprefix("mac_exit="))
        remote.state = RemoteState.EXITED
        if descriptor := os.environ.get(MAC_GATE_EXIT_STATUS_FD):
            _ = os.write(int(descriptor), f"{status}\n".encode("ascii"))
        return status
    print("mac queue: the Mac returned no exit status", file=sys.stderr)
    return ssh_status or 255

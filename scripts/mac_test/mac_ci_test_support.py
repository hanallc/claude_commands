"""Private SSH replies for Mac CI process reads."""

from pathlib import Path
import shlex
import subprocess
import sys

from scripts.mac_test.mac_ci import PROCESS_LIST_COMMAND


IDLE_PROCESS_LIST = "1 Ss launchd\n"
BUSY_PROCESS_LIST = IDLE_PROCESS_LIST + "43 ? Runner.Worker\n44 S Runner.Worker\n"


def idle_ci_reply(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
    assert arguments[0] == "ssh" and arguments[-1] == PROCESS_LIST_COMMAND, arguments
    return subprocess.CompletedProcess(arguments, 0, IDLE_PROCESS_LIST, "")


def write_idle_ci_ssh(path: Path) -> None:
    """Install a private SSH program that answers only the CI process read."""
    _ = path.write_text(f"#!{sys.executable}\n" + idle_ci_python_reply()
                        + 'raise SystemExit("Unexpected SSH command: " + sys.argv[-1])\n')
    path.chmod(0o755)


def idle_ci_python_reply() -> str:
    """Answer only the CI read in a Python SSH stub."""
    return ("import sys\n"
            f"if sys.argv[-1] == {PROCESS_LIST_COMMAND!r}:\n"
            f"    sys.stdout.write({IDLE_PROCESS_LIST!r})\n"
            "    raise SystemExit(0)\n")


def idle_ci_shell_reply(last_argument: str) -> str:
    """Answer the CI read before the fixture records other commands."""
    return (f'\nif [ "${{0##*/}}" = ssh ] && [ "{last_argument}" = {shlex.quote(PROCESS_LIST_COMMAND)} ]; then\n'
            f'    printf "%s" {shlex.quote(IDLE_PROCESS_LIST)}\n'
            '    exit 0\nfi\n')

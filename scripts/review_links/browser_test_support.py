"""Start and stop the private browsers that page tests use."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import IO, cast
from uuid import uuid4

from scripts.lib.process_group import send_process_group
from scripts.message import darwin_test_processes
from scripts.message.process_test_support import OWNERS


class NoDetachedCrashpad(Enum):
    EXPECTED = "no detached Crashpad handler expected"


@dataclass(frozen=True)
class PrivateCrashpad:
    database: Path
    owner_token: str


@dataclass(frozen=True)
class BrowserProcess[Output: (bytes, str)]:
    """A browser that owns a private process group."""

    process: subprocess.Popen[Output]
    crashpad: PrivateCrashpad | NoDetachedCrashpad


def start_browser[Output: (bytes, str)](
        command: Sequence[str], *, output: type[Output], stdin: int | None = None,
        stdout: int | IO[bytes] | None = None, stderr: int | IO[bytes] | None = None,
        bufsize: int = -1, env: Mapping[str, str] | None = None) -> BrowserProcess[Output]:
    crashpad: PrivateCrashpad | NoDetachedCrashpad = NoDetachedCrashpad.EXPECTED
    # macOS has no parent-death signal and relies on normal cleanup.
    if sys.platform == "linux":
        # setpriv arms SIGKILL before the shell checks the parent and executes the browser.
        command = ["setpriv", "--pdeathsig", "KILL", "sh", "-c",
                   '[ "$PPID" = "$1" ] || exit 1; shift; exec "$@"',
                   "browser-test", str(os.getpid()), *command]
    elif sys.platform == "darwin":
        for argument in command:
            profile = argument.removeprefix("--user-data-dir=")
            if profile == argument:
                continue
            crashpad = PrivateCrashpad(Path(profile).resolve() / "Crashpad", uuid4().hex)
            environment = os.environ if env is None else env
            # Chrome uses its default crash directory unless this override is set.
            env = {**environment, "BREAKPAD_DUMP_LOCATION": str(crashpad.database),
                   OWNERS: environment.get(OWNERS, "") + ":" + crashpad.owner_token}
            break
    process = subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=stderr,
                               text=output is str, bufsize=bufsize, env=env, start_new_session=True)
    return BrowserProcess(cast("subprocess.Popen[Output]", process), crashpad)


def _stop_crashpad(crashpad: PrivateCrashpad) -> None:
    """Stop the Mac handlers that use this fixture's private database."""
    table = subprocess.run(["/bin/ps", "-A", "-ww", "-o", "pid=", "-o", "command="],
                           capture_output=True, text=True, errors="replace", check=True, timeout=5)
    handlers: list[darwin_test_processes.OwnedProcessIdentity] = []
    database = f" --database={crashpad.database} --"
    for line in table.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2 or database not in fields[1]:
            continue
        pid, command = fields
        executable, _, _ = command.partition(" --")
        if Path(executable).name != "chrome_crashpad_handler":
            continue
        owned = darwin_test_processes.identity(int(pid))
        # Check the marker before signaling through the captured process identity.
        if owned is not None and darwin_test_processes.has_owner(owned.pid, crashpad.owner_token):
            darwin_test_processes.signal_identity(owned, signal.SIGTERM)
            handlers.append(owned)
    if handlers:
        time.sleep(0.1)
        for owned in handlers:
            darwin_test_processes.signal_identity(owned, signal.SIGKILL)


def stop_browser[Output: (bytes, str)](browser: BrowserProcess[Output]) -> None:
    """Stop the browser group and close its streams."""
    process = browser.process
    try:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                send_process_group(process.pid, signal.SIGTERM)
            with suppress(subprocess.TimeoutExpired):
                _ = process.wait(timeout=10)
        # A child can remain in the group after the browser exits.
        with suppress(ProcessLookupError):
            send_process_group(process.pid, signal.SIGKILL)
        _ = process.wait(timeout=10)
        if isinstance(browser.crashpad, PrivateCrashpad):
            _stop_crashpad(browser.crashpad)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def run_browser(command: Sequence[str], *, timeout: float, check: bool = False,
                env: Mapping[str, str] | None = None, stdout: int = subprocess.PIPE,
                stderr: int = subprocess.PIPE) -> subprocess.CompletedProcess[str]:
    """Run a browser and collect its text output."""
    browser = start_browser(command, output=str, stdout=stdout, stderr=stderr, env=env)
    try:
        output, errors = browser.process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        error.cmd = command
        raise
    finally:
        stop_browser(browser)
    result = subprocess.CompletedProcess(command, browser.process.returncode, output, errors)
    if check:
        result.check_returncode()
    return result

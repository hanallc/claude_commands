"""Capture nextest output on the Mac and protect logs during use."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Collection, Generator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO

if __name__ == "__main__":
    with (Path(__file__).resolve().parents[1] / "lib/package_root.py").open() as package_root:
        exec(package_root.read(), {"__file__": package_root.name})

from scripts.buildlog import parse
from scripts.lib import conf

DIRECTORY = ".local/state/mac-test/logs"
LOG_NAME = re.compile(r"nextest\.[a-z0-9_]{8}", re.IGNORECASE)
RETENTION_DAYS = "nextest_failure_log_days"
TAIL_BYTES = "nextest_failure_tail_bytes"
SUMMARY_BYTES = "nextest_failure_summary_bytes"
DEFAULT_SETTINGS = Path(__file__).resolve().parents[2] / "config/mac_test.conf"
SETUP_FAILURE = os.EX_TEMPFAIL


@dataclass(frozen=True)
class NextestOutputLimits:
    tail_bytes: int
    summary_bytes: int


def output_limits(settings: conf.ConfigRead | conf.UseConfiguredValue, keys: Collection[str]) -> NextestOutputLimits:
    """Use the typed reader and keep the shipped Summary ceiling."""
    defaults = conf.read(DEFAULT_SETTINGS)
    ceiling = defaults.whole(SUMMARY_BYTES)
    tail = settings.whole(TAIL_BYTES) if isinstance(settings, conf.ConfigRead) and TAIL_BYTES in keys else defaults.whole(TAIL_BYTES)
    summary = settings.whole(SUMMARY_BYTES) if isinstance(settings, conf.ConfigRead) and SUMMARY_BYTES in keys else ceiling
    if tail < 1 or not 1 <= summary <= ceiling:
        raise conf.Unusable(f"Mac nextest output limits must be positive and the Summary limit must be at most {ceiling}")
    return NextestOutputLimits(tail, summary)


def capture_function(limits: NextestOutputLimits) -> str:
    """Define the remote shell function that executes the log writer."""
    return (
        'run_nextest_logged() { '
        'nextest_python="$HOME/.claude/scripts/lib/py"; '
        'nextest_writer="$HOME/.claude/scripts/mac_test/nextest_log.py"; '
        'if [ ! -x "$nextest_python" ] || [ ! -f "$nextest_writer" ]; then '
        "printf 'mac_test: the nextest log writer is unavailable\\n' >&2; "
        f'return {SETUP_FAILURE}; fi; '
        f'"$nextest_python" -B "$nextest_writer" run {limits.tail_bytes} {limits.summary_bytes} "$@"; }}'
    )


class LogAccess(Enum):
    READ = fcntl.LOCK_SH
    REMOVE = fcntl.LOCK_EX


@contextmanager
def locked_log(folder: int, name: str, access: LogAccess) -> Generator[int]:
    """Lock the current regular file and hold the lock until the descriptor closes."""
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise FileNotFoundError(name)
        fcntl.flock(descriptor, access.value | fcntl.LOCK_NB)
        if not os.path.samestat(os.fstat(descriptor), os.stat(name, dir_fd=folder, follow_symlinks=False)):
            raise FileNotFoundError(name)
        yield descriptor
    finally:
        os.close(descriptor)


def read_log(path: Path, output: BinaryIO) -> None:
    """Copy a retained log while holding a shared lock."""
    folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with locked_log(folder, path.name, LogAccess.READ) as descriptor:
            with os.fdopen(descriptor, "rb", closefd=False) as log:
                shutil.copyfileobj(log, output)
    finally:
        os.close(folder)


def replay_log(log: BinaryIO, path: Path, status: int, limits: NextestOutputLimits, output: BinaryIO) -> None:
    """Send the whole log after success or a bounded tail and Summary after failure."""
    size = log.seek(0, os.SEEK_END)
    start = max(0, size - limits.tail_bytes) if status else 0
    _ = log.seek(start)
    if status:
        _ = output.write(f"mac_test: full nextest log: {path}\n".encode("utf-8", errors="replace"))
        if start:
            _ = output.write(b"mac_test: tail fragment: ")
        tail = log.read(size - start)
        _ = output.write(tail)
        if tail and not tail.endswith(b"\n"):
            _ = output.write(b"\n")
    else:
        shutil.copyfileobj(log, output)
        if size:
            _ = log.seek(-1, os.SEEK_END)
            if log.read(1) != b"\n":
                _ = output.write(b"\n")
    if status:
        _ = log.seek(0)
        summary = b""
        for raw in log:
            line = parse.clean_line(raw.decode("utf-8", errors="replace").removesuffix("\n"))
            if parse.SUMMARY.match(line):
                summary = line.encode()[:limits.summary_bytes]
        if summary:
            _ = output.write(summary + b"\n")


def run_logged(command: Sequence[str], limits: NextestOutputLimits, output: BinaryIO) -> int:
    """Hold the log lock through dispatch and replay."""
    from scripts.copies import copies

    root = Path.home() / DIRECTORY
    with ExitStack() as files:
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if root.is_symlink():
                raise OSError(f"{root} is a symbolic link")
            descriptor, name = tempfile.mkstemp(prefix="nextest.", dir=root)
            path = Path(name)
            _ = files.callback(os.close, descriptor)
            try:
                log = files.enter_context(os.fdopen(descriptor, "w+b", closefd=False))
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                status = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                        pass_fds=copies.inherited_mirror_descriptors(), check=False).returncode
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                raise
        except (OSError, ValueError) as error:
            print(f"mac_test: cannot start nextest with a log: {error}", file=sys.stderr)
            return SETUP_FAILURE
        try:
            if status:
                os.utime(log.fileno(), None)
            replay_log(log, path, status, limits, output)
            if status == 0:
                path.unlink()
        except OSError as error:
            print(f"mac_test: cannot replay the nextest log: {error}", file=sys.stderr)
    return 128 - status if status < 0 else status


def main(arguments: list[str]) -> int:
    if arguments[:1] == ["run"] and len(arguments) >= 4:
        return run_logged(arguments[3:], NextestOutputLimits(int(arguments[1]), int(arguments[2])), sys.stdout.buffer)
    if arguments[:1] == ["read"] and len(arguments) == 2:
        try:
            read_log(Path(arguments[1]), sys.stdout.buffer)
        except OSError as error:
            print(f"mac_test: cannot read the nextest log: {error}", file=sys.stderr)
            return 1
        return 0
    print("usage: nextest_log.py read PATH | run TAIL_BYTES SUMMARY_BYTES COMMAND...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

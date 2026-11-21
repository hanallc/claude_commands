"""Identify the report written during one Cargo invocation."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile

if __name__ == "__main__":
    with (Path(__file__).resolve().parents[1] / "lib/package_root.py").open() as package_root:
        exec(package_root.read(), {"__file__": package_root.name})

from scripts.buildlog import step_context, store


TIMING_REPORT_PREFIX = "timings: "
REMOTE_TIMING_REPORT_PREFIX = "cargo_timing_report="
TIMING_TOKEN_OPTION = "--timing-token"
STAMPED_TIMING_REPORT = "cargo-timing-*.html"


@dataclass(frozen=True)
class TimingRunToken:
    """Identify timing records from one SSH runner."""

    value: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", self.value):
            raise ValueError("Invalid timing run token")

    @classmethod
    def new(cls) -> TimingRunToken:
        return cls(secrets.token_hex(16))

    def argument(self) -> str:
        return f"{TIMING_TOKEN_OPTION}={self.value}"

    def record_prefix(self) -> str:
        return f"{REMOTE_TIMING_REPORT_PREFIX}{self.value}:"


class UntimedRun:
    """The runner has no timing token."""


UNTIMED_RUN = UntimedRun()


def take_timing_argument(command: list[str]) -> tuple[TimingRunToken | UntimedRun, list[str]]:
    """Remove the runner's timing argument before starting the child."""
    option = TIMING_TOKEN_OPTION + "="
    if command and command[0].startswith(option):
        return TimingRunToken(command[0].removeprefix(option)), command[1:]
    return UNTIMED_RUN, command


@dataclass(frozen=True)
class ConfirmedTimingReport:
    """Name a report file confirmed on the host that holds it."""

    host: str
    path: str

    def location(self) -> str:
        return f"{self.host}:{self.path}"


class NoTimingReport:
    """No single new timing report can be identified."""


class UnrecognizedTimingRecord:
    """The line is ordinary output."""


def timing_files(target: Path) -> frozenset[Path]:
    """List stamped Cargo reports and exclude the reusable latest alias."""
    directory = target / "cargo-timings"
    try:
        directory_mode = directory.lstat().st_mode
    except FileNotFoundError:
        return frozenset()
    if not stat.S_ISDIR(directory_mode):
        raise ValueError("The Cargo timing path is not a directory")
    return frozenset(
        path for path in directory.iterdir()
        if path.match(STAMPED_TIMING_REPORT) and stat.S_ISREG(path.lstat().st_mode)
    )


@dataclass(frozen=True)
class TimingDirectorySnapshot:
    """Record the reports present before a Cargo command starts."""

    target: Path
    existing: frozenset[Path]

    @classmethod
    def before_run(cls, target: Path) -> TimingDirectorySnapshot | NoTimingReport:
        try:
            return cls(target, timing_files(target))
        except (OSError, ValueError):
            return NoTimingReport()

    def new_report(self) -> Path | NoTimingReport:
        try:
            added = timing_files(self.target) - self.existing
        except (OSError, ValueError):
            return NoTimingReport()
        if len(added) == 1:
            return next(iter(added))
        return NoTimingReport()


def format_timing_report(report: ConfirmedTimingReport) -> str:
    return TIMING_REPORT_PREFIX + report.location()


def describe_local_report(report: Path) -> str:
    return format_timing_report(ConfirmedTimingReport(store.host_name(), str(report)))


def snapshot_for_command(command: list[str]) -> TimingDirectorySnapshot | NoTimingReport:
    """Resolve the target on the execution host before the command runs."""
    target = step_context.resolve_target_folder(command, os.getcwd(), os.environ)
    if isinstance(target, step_context.CargoTargetFolder):
        return TimingDirectorySnapshot.before_run(target.path)
    return NoTimingReport()


def cargo_arguments(verb: str, words: Sequence[str]) -> list[str]:
    invocation = ["cargo", "nextest", "run"] if verb == "nextest" else ["cargo", verb]
    return [*invocation, *words]


def timings_requested(verb: str, words: Sequence[str]) -> bool:
    options = words[:words.index("--")] if "--" in words else words
    return verb in ("build", "nextest") and "--timings" in options


def mac_build_prefix(timing: TimingRunToken | UntimedRun) -> str:
    """Wrap a timed build on the Mac."""
    if isinstance(timing, UntimedRun):
        return ""
    return ('"$HOME/.claude/scripts/lib/py" -B "$HOME/.claude/scripts/mac_test/cargo_timings.py" '
            + f"'' {timing.argument()} ")


def retain_report(report: Path | NoTimingReport, cleanup_target: Path) -> Path | NoTimingReport:
    """Move the report outside the target before cleanup can remove it."""
    if isinstance(report, NoTimingReport):
        return report
    with suppress(OSError):
        cleanup_target = cleanup_target.resolve()
        if not report.is_relative_to(cleanup_target):
            return report
        root = Path.home() / ".local/state/build-target/cargo-timings"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or root.resolve().is_relative_to(cleanup_target):
            return NoTimingReport()
        folder = Path(tempfile.mkdtemp(prefix="run-", dir=root))
        try:
            return Path(shutil.move(report, folder / report.name))
        except OSError:
            with suppress(OSError):
                folder.rmdir()
    return NoTimingReport()


def validate_remote_location(host: str, path_text: str) -> ConfirmedTimingReport | NoTimingReport:
    """Validate the short host name and absolute Cargo report path."""
    if not re.fullmatch(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?", host):
        return NoTimingReport()
    path = PurePosixPath(path_text)
    if (not path_text.isprintable() or not path.is_absolute() or ".." in path.parts
            or not path.match(STAMPED_TIMING_REPORT) or path_text.endswith("/")):
        return NoTimingReport()
    return ConfirmedTimingReport(host, path_text)


def remote_report_line(timing: TimingRunToken, report: Path | NoTimingReport) -> str:
    """Encode one timing record before the final SSH status."""
    value = "none"
    if isinstance(report, Path):
        with suppress(OSError):
            if report.is_file():
                location = validate_remote_location(store.host_name(), str(report))
                if isinstance(location, ConfirmedTimingReport):
                    value = location.location()
    # Terminate any partial child output before the record.
    return f"\n{timing.record_prefix()}{value}\n"


def parse_remote_report(
    timing: TimingRunToken, record: str,
) -> ConfirmedTimingReport | NoTimingReport | UnrecognizedTimingRecord:
    """Read the report location from a timing record."""
    prefix = timing.record_prefix()
    if not record.startswith(prefix):
        return UnrecognizedTimingRecord()
    value = record.removeprefix(prefix)
    if value == "none":
        return NoTimingReport()
    host, _, path = value.partition(":")
    location = validate_remote_location(host, path)
    return location if isinstance(location, ConfirmedTimingReport) else UnrecognizedTimingRecord()


def mac_report_line(timing: TimingRunToken, before: TimingDirectorySnapshot | NoTimingReport) -> str:
    report = before.new_report() if isinstance(before, TimingDirectorySnapshot) else before
    if cleanup_target := os.environ.get("VERIFY_MAC_TIMING_CLEANUP_TARGET"):
        report = retain_report(report, Path(cleanup_target))
    return remote_report_line(timing, report)


def main(argv: list[str]) -> int:
    target, *command = argv
    timing: TimingRunToken | UntimedRun = UNTIMED_RUN
    if target:
        before = TimingDirectorySnapshot.before_run(Path(target))
    else:
        timing, command = take_timing_argument(command)
        before = snapshot_for_command(cargo_arguments(command[1], command[2:]))
    try:
        # Keep the same lease descriptors as a direct exec.
        status = subprocess.run(command, close_fds=False, check=False).returncode
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        status = 127
        before = NoTimingReport()
    except OSError as error:
        print(str(error), file=sys.stderr)
        status = 126
        before = NoTimingReport()
    if isinstance(timing, TimingRunToken):
        print(mac_report_line(timing, before), end="", flush=True)
    elif target and isinstance(before, TimingDirectorySnapshot) and isinstance(report := before.new_report(), Path):
        print(describe_local_report(report), flush=True)
    return status if status >= 0 else 128 - status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

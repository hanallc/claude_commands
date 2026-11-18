#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Hold one jobserver token for each nextest test thread.

invoke.sh runs this inside a test step's scope when CARGO_MAKEFLAGS names a
jobserver pool (natedev's /dev/steve). It builds the tests first with
`--no-run`, so the compile jobs take tokens as cargo always does and the step
holds no test token while it compiles. Then it takes `test_token_floor`
tokens, and waits for them when the pool has fewer free. After that it takes
free tokens without a wait, up to the step's width. The tests run as a child
with one thread for each token held. Every token goes back when the child
ends. The holder sets no signal handler: a signal ends it with its default
action, steve takes back the tokens of a process that ends, and the child
ends with the step's process group.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.build_queue import progress_profile
from scripts.lib import conf
from scripts.lint import nextest_budget as budget

# `cargo nextest run` options that only change how tests run or report: the runner,
# stress and reporter options of `cargo nextest run --help`. nextest refuses some of
# them with --no-run (--fail-fast, --no-fail-fast, --max-fail, --debugger and --tracer
# in 0.9.143), so the build drops all of them.
RUN_ONLY_VALUE_OPTIONS = frozenset({
    "-j", "--test-threads", "--retries", "--flaky-result", "--max-fail", "--debugger", "--tracer",
    "--no-tests", "--stress-count", "--stress-duration", "--failure-output", "--success-output",
    "--status-level", "--final-status-level", "--show-progress", "--max-progress-running",
    "--message-format", "--message-format-version",
})
RUN_ONLY_FLAGS = frozenset({
    "--fail-fast", "--no-fail-fast", "--no-capture", "--nocapture", "--no-output-indent",
    "--hide-progress-bar", "--no-input-handler",
})


CARGO_BUILD_VALUE_OPTIONS = frozenset({
    "-p", "--package", "--exclude", "--bin", "--example", "--test", "--bench", "-F", "--features",
    "--build-jobs", "--cargo-profile", "--target", "--target-dir", "--cargo-message-format", "-Z",
})
CARGO_BUILD_FLAGS = frozenset({
    "--workspace", "--all", "--lib", "--bins", "--examples", "--tests", "--benches", "--all-targets",
    "--all-features", "--no-default-features", "-r", "--release", "--unit-graph", "--timings",
    "--frozen", "--locked", "--offline", "--cargo-quiet", "--cargo-verbose", "--ignore-rust-version",
    "--future-incompat-report",
})


def reuse_selection(arguments: Sequence[str]) -> list[str]:
    """Keep nextest options that apply to an existing binary list."""
    kept: list[str] = []
    skip_value = False
    for index, argument in enumerate(arguments):
        if skip_value:
            skip_value = False
            continue
        if argument == "--":
            kept.extend(arguments[index:])
            break
        name, equal, _ = argument.partition("=")
        if name in CARGO_BUILD_VALUE_OPTIONS:
            skip_value = not equal
        elif name not in CARGO_BUILD_FLAGS and not (
                len(argument) > 2 and argument[:2] in ("-p", "-F", "-Z")):
            kept.append(argument)
    return kept


def build_selection(arguments: Sequence[str]) -> list[str]:
    """Keep the options that choose what to build; drop the run-only options and their values."""
    kept: list[str] = []
    skip_value = False
    for argument in arguments:
        if skip_value:
            skip_value = False
            continue
        name = argument.partition("=")[0]
        if argument in RUN_ONLY_FLAGS or name in RUN_ONLY_VALUE_OPTIONS and "=" in argument:
            continue
        if argument in RUN_ONLY_VALUE_OPTIONS:
            skip_value = True
            continue
        if argument.startswith("-j") and argument[2:].isdecimal():
            continue
        kept.append(argument)
    return kept


@dataclass(frozen=True)
class SizedTestRun:
    """`cargo nextest run --test-threads WIDTH ARGS...`, the shape run_nextest passes."""

    head: tuple[str, ...]
    width: int
    rest: tuple[str, ...]

    def build_only(self) -> list[str]:
        """Build every test binary the run needs; run-only and test binary arguments do not apply."""
        selection = self.rest[:self.rest.index("--")] if "--" in self.rest else self.rest
        return [*self.head, "--no-run", *build_selection(selection)]

    def with_threads(self, threads: int) -> list[str]:
        return [*self.head, "--test-threads", str(threads), *self.rest]


@dataclass(frozen=True)
class UnsizedCommand:
    """Any other command: it runs as given and holds no token."""

    argv: tuple[str, ...]


def test_run(argv: Sequence[str]) -> SizedTestRun | UnsizedCommand:
    head, rest = tuple(argv[:3]), tuple(argv[5:])
    selection = rest[:rest.index("--")] if "--" in rest else rest
    if (head == ("cargo", "nextest", "run") and len(argv) >= 5 and argv[3] == "--test-threads"
            and argv[4].isdecimal() and int(argv[4]) > 0 and "--no-run" not in selection):
        return SizedTestRun(head, int(argv[4]), rest)
    return UnsizedCommand(tuple(argv))


class TokenPool:
    """An open jobserver pool and the tokens this run holds from it."""

    def __init__(self, fd: int) -> None:
        self.fd: int = fd
        self.held: bytearray = bytearray()

    def take_free(self, limit: int) -> None:
        """Take tokens that are free now, until this run holds `limit`."""
        os.set_blocking(self.fd, False)
        try:
            while len(self.held) < limit:
                self.held += self._read()
        except BlockingIOError:
            pass
        finally:
            os.set_blocking(self.fd, True)

    def take_waiting(self, count: int) -> None:
        """Wait until this run holds `count` tokens."""
        while len(self.held) < count:
            self.held += self._read()

    def _read(self) -> bytes:
        token = os.read(self.fd, 1)
        if not token:
            raise OSError("the jobserver pool closed")
        return token

    def give_back(self) -> None:
        """Write every held token back and close the pool."""
        try:
            while self.held:
                written = os.write(self.fd, self.held)
                del self.held[:written]
        finally:
            os.close(self.fd)


def prepare_binaries(argv: Sequence[str], binaries: Path) -> int:
    """Build once and write the binary list before the build grant ends."""
    args = argv[3:]
    selection = args[:args.index("--")] if "--" in args else args
    command = [*argv[:2], "list", "--list-type", "binaries-only", "--message-format", "json",
               *build_selection(selection)]
    with binaries.open("w", encoding="utf-8") as output:
        return exit_status(subprocess.run(command, stdout=output, check=False).returncode)


def exit_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def run_child(argv: Sequence[str]) -> int:
    """Run one child in this process group, and end when it ends."""
    return exit_status(subprocess.run(argv, check=False).returncode)


def record_reservation_width(reservation: str, threads: int) -> None:
    """Give the step's memory reservation the width its tests run at."""
    from scripts.build_queue import admission_client

    _ = admission_client.width(reservation, threads)


def thread_line(threads: int, width: int) -> str:
    line = budget.thread_line(budget.SpecifiedTestWidth(str(threads)), budget.SpecifiedTestWidth(str(width)))
    return f"{line}; one jobserver token for each thread"


def run_with_tokens(run: SizedTestRun, pool_path: Path, floor: int, width_file: Path | None,
                    record_width: Callable[[int], None], *, reuse_prepared_binaries: bool = False) -> int:
    """Take tokens after the build. Run one test thread for each token."""
    if not reuse_prepared_binaries:
        status = run_child(run.build_only())
        if status != 0:
            return status
    try:
        pool = TokenPool(os.open(pool_path, os.O_RDWR | os.O_CLOEXEC))
    except OSError as error:
        print(f"nextest: {run.width} test threads with no jobserver tokens: {pool_path}: {error}", flush=True)
        return run_child(run.with_threads(run.width))
    try:
        wanted = min(floor, run.width)
        pool.take_free(wanted)
        if len(pool.held) < wanted:
            # The Builds page reads this line as the token wait stage.
            print(progress_profile.token_wait_line(wanted - len(pool.held)), flush=True)
            pool.take_waiting(wanted)
        pool.take_free(run.width)
        threads = len(pool.held)
        if width_file is not None:
            _ = width_file.write_text(f"{threads}\n", encoding="utf-8")
        if threads != run.width:
            record_width(threads)
        print(thread_line(threads, run.width), flush=True)
        return run_child(run.with_threads(threads))
    finally:
        pool.give_back()


class Arguments(argparse.Namespace):
    pool: Path | None = None
    prepare_binaries: Path | None = None
    reuse_binaries: Path | None = None
    width_file: Path | None = None
    reservation: str = ""
    command: list[str] = []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hold one jobserver token for each nextest test thread.")
    _ = parser.add_argument("--pool", type=Path)
    phase = parser.add_mutually_exclusive_group()
    _ = phase.add_argument("--prepare-binaries", type=Path)
    _ = phase.add_argument("--reuse-binaries", type=Path)
    _ = parser.add_argument("--width-file", type=Path)
    _ = parser.add_argument("--reservation", default="")
    _ = parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv, namespace=Arguments())
    pool_path, width_file, reservation, command = args.pool, args.width_file, args.reservation, args.command
    if command[:1] == ["--"]:
        command = command[1:]
    def record_width(threads: int) -> None:
        if reservation:
            record_reservation_width(reservation, threads)

    if args.prepare_binaries is not None:
        return prepare_binaries(command, args.prepare_binaries)
    if args.reuse_binaries is not None:
        selection = reuse_selection(command[3:])
        # Keep the explicit width at the front for the token holder.
        position = 5 if selection[:1] == ["--test-threads"] else 3
        command = [*command[:3], *selection]
        command[position:position] = ["--binaries-metadata", str(args.reuse_binaries)]
    run = test_run(command)
    if pool_path is None or isinstance(run, UnsizedCommand):
        return run_child(command)
    floor = conf.read("build_admission").whole("test_token_floor")
    return run_with_tokens(run, pool_path, floor, width_file, record_width,
                           reuse_prepared_binaries=args.reuse_binaries is not None)


if __name__ == "__main__":
    sys.exit(main())

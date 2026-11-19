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
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.buildlog import parse
from scripts.build_queue import admission_client
from scripts.build_queue import progress_profile
from scripts.lib import conf
from scripts.lint import nextest_budget as budget

# `cargo nextest run` options that only change how tests run or report: the runner,
# stress and reporter options of `cargo nextest run --help`. nextest refuses some of
# them with --no-run (--fail-fast, --no-fail-fast, --max-fail, --debugger and --tracer
# in 0.9.143), so the build drops all of them.
RUN_ONLY_VALUE_OPTIONS = frozenset({
    "-j", "--jobs", "--test-threads", "--retries", "--flaky-result", "--max-fail", "--debugger", "--tracer",
    "--no-tests", "--stress-count", "--stress-duration", "--failure-output", "--success-output",
    "--status-level", "--final-status-level", "--show-progress", "--max-progress-running",
    "--message-format", "--message-format-version",
})
RUN_ONLY_FLAGS = frozenset({
    "--fail-fast", "--ff", "--no-fail-fast", "--nff", "--no-capture", "--nocapture", "--no-output-indent",
    "--hide-progress-bar", "--no-input-handler",
})

CARGO_ONLY_VALUE_OPTIONS = frozenset({
    "--package", "-p", "--exclude", "--bin", "--example", "--test", "--bench", "--features", "-F",
    "--build-jobs", "--cargo-profile", "--target", "--target-dir", "--cargo-message-format", "-Z",
})
CARGO_ONLY_FLAGS = frozenset({
    "--workspace", "--all", "--lib", "--bins", "--examples", "--tests", "--benches", "--all-targets",
    "--all-features", "--no-default-features", "--release", "-r", "--unit-graph", "--timings",
    "--frozen", "--locked", "--offline", "--cargo-quiet", "--cargo-verbose", "--ignore-rust-version",
    "--future-incompat-report",
})
SHARED_VALUE_OPTIONS = frozenset({
    "--manifest-path", "--config", "--color", "--config-file", "--user-config-file", "--tool-config-file",
    "-P", "--profile", "-E", "--filterset", "--run-ignored", "--partition", "--platform-filter",
    "--archive-file", "--archive-format", "--extract-to", "--workspace-remap", "--cargo-metadata",
    "--binaries-metadata", "--target-dir-remap", "--build-dir-remap",
})

def without_options(arguments: Sequence[str], values: frozenset[str], flags: frozenset[str]) -> list[str]:
    """Remove the given options and preserve their values only when the options stay."""
    kept: list[str] = []
    remaining = iter(arguments)
    for argument in remaining:
        if argument == "--":
            kept.extend((argument, *remaining))
            break
        name, separator, _value = argument.partition("=")
        if name in flags:
            continue
        if name in values:
            if not separator:
                _ = next(remaining, None)
            continue
        if any(short in values and argument.startswith(short) and argument != short
               for short in ("-j", "-p", "-F", "-Z")):
            continue
        kept.append(argument)
        if not separator and name in (SHARED_VALUE_OPTIONS | RUN_ONLY_VALUE_OPTIONS | CARGO_ONLY_VALUE_OPTIONS):
            value = next(remaining, None)
            if value is not None:
                kept.append(value)
    return kept


def build_selection(arguments: Sequence[str]) -> list[str]:
    """Keep the build and selection options."""
    return without_options(arguments, RUN_ONLY_VALUE_OPTIONS, RUN_ONLY_FLAGS)


def nextest_inputs(arguments: Sequence[str]) -> Sequence[str]:
    """Return the arguments before nextest's test binary arguments."""
    return arguments[:arguments.index("--")] if "--" in arguments else arguments


@dataclass(frozen=True)
class SizedTestRun:
    """`cargo nextest run --test-threads WIDTH ARGS...`, the shape run_nextest passes."""

    head: tuple[str, ...]
    width: int
    rest: tuple[str, ...]

    def build_only(self) -> list[str]:
        """Build every test binary the run needs; run-only and test binary arguments do not apply."""
        return [*self.head, "--no-run", *build_selection(nextest_inputs(self.rest))]

    def with_threads(self, threads: int) -> list[str]:
        return [*self.head, "--test-threads", str(threads), *self.rest]


@dataclass(frozen=True)
class UnsizedCommand:
    """Any other command: it runs as given and holds no token."""

    argv: tuple[str, ...]


def test_run(argv: Sequence[str]) -> SizedTestRun | UnsizedCommand:
    head, rest = tuple(argv[:3]), tuple(argv[5:])
    selection = nextest_inputs(rest)
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


def exit_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def run_child(argv: Sequence[str]) -> int:
    """Run one child in this process group, and end when it ends."""
    return exit_status(subprocess.run(argv, check=False).returncode)


def record_reservation_width(reservation: str, threads: int) -> None:
    """Give the step's memory reservation the width its tests run at."""
    _ = admission_client.width(reservation, threads)


def thread_line(threads: int, width: int) -> str:
    line = budget.thread_line(budget.SpecifiedTestWidth(str(threads)), budget.SpecifiedTestWidth(str(width)))
    return f"{line}; one jobserver token for each thread"


def run_with_tokens(run: SizedTestRun, pool_path: Path, floor: int, width_file: Path | None,
                    record_width: Callable[[int], None]) -> int:
    """Build, take the tokens, then run the tests at one thread for each token."""
    status = run_child(run.build_only())
    if status != 0:
        return status
    return run_built_with_tokens(run, pool_path, floor, width_file, record_width)


def run_built_with_tokens(run: SizedTestRun, pool_path: Path, floor: int, width_file: Path | None,
                          record_width: Callable[[int], None]) -> int:
    """Take tokens and run the tests at one thread for each token."""
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


def can_list(command: Sequence[str]) -> bool:
    """Leave build-only, rerun, and archive commands on their original path."""
    excluded = {"--help", "-h", "--version", "-V", "--no-run", "--unit-graph", "--rerun",
                "--archive-file", "--binaries-metadata", "--cargo-message-format"}
    return (list(command[:3]) == ["cargo", "nextest", "run"]
            and all(word.partition("=")[0] not in excluded and not word.startswith("-R")
                    for word in nextest_inputs(command[3:])))


def run_with_selection(command: Sequence[str], selection_file: Path,
                       run_built: Callable[[Sequence[str]], int]) -> int:
    """Build and save the selection before running the same binaries."""
    arguments = command[3:]
    list_command = [*command[:2], "list", "--message-format=json", *build_selection(arguments)]
    completed = subprocess.run(list_command, stdout=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        return exit_status(completed.returncode)
    try:
        listing = parse.nextest_listing(cast(dict[str, object], json.loads(completed.stdout)))
        _ = selection_file.write_text(json.dumps(listing.selection.by_binary, separators=(",", ":")), encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="nextest-binaries-") as folder:
            metadata = Path(folder) / "binaries.json"
            _ = metadata.write_text(json.dumps(listing.binaries_metadata, separators=(",", ":")), encoding="utf-8")
            reused = without_options(arguments, CARGO_ONLY_VALUE_OPTIONS, CARGO_ONLY_FLAGS)
            # Cargo metadata for the reused binaries must also respect offline mode.
            position = reused.index("--") if "--" in reused else len(reused)
            if {"--offline", "--frozen"}.intersection(nextest_inputs(arguments)):
                reused[position:position] = ["--config", "net.offline=true"]
            reused[position:position] = ["--binaries-metadata", str(metadata)]
            return run_built([*command[:3], *reused])
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"nextest: cannot save the test selection: {error}", file=sys.stderr, flush=True)
        return 2


class Arguments(argparse.Namespace):
    pool: Path | None = None
    width_file: Path | None = None
    selection_file: Path | None = None
    reservation: str = ""
    command: list[str] = []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hold one jobserver token for each nextest test thread.")
    _ = parser.add_argument("--pool", type=Path)
    _ = parser.add_argument("--width-file", type=Path)
    _ = parser.add_argument("--selection-file", type=Path)
    _ = parser.add_argument("--reservation", default="")
    _ = parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv, namespace=Arguments())
    pool_path, width_file, reservation, command = args.pool, args.width_file, args.reservation, args.command
    if command[:1] == ["--"]:
        command = command[1:]
    def record_width(threads: int) -> None:
        if reservation:
            record_reservation_width(reservation, threads)

    def execute(words: Sequence[str], *, binaries_ready: bool) -> int:
        run = test_run(words)
        if pool_path is None or isinstance(run, UnsizedCommand):
            return run_child(words)
        floor = conf.read("build_admission").whole("test_token_floor")
        launch = run_built_with_tokens if binaries_ready else run_with_tokens
        return launch(run, pool_path, floor, width_file, record_width)

    if args.selection_file is not None and can_list(command):
        return run_with_selection(command, args.selection_file, lambda words: execute(words, binaries_ready=True))
    return execute(command, binaries_ready=False)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Share the jobserver between nextest test threads and nested Cargo jobs.

Build and list the tests before taking tokens. Limit total tokens to the
requested width and the selected test count plus `nested_tokens`. Wait for
`test_token_floor` test tokens, up to the selected test count and requested width.
Take more free tokens up to the limit. Give nested Cargo up to `nested_tokens`
of the tokens above the test floor. Give one remaining token to each test thread.
Return all shared tokens when nextest exits. If a signal ends the launcher,
steve reclaims its tokens and the step group ends the test child.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.build_queue import progress_profile
from scripts.lib import conf, process_start
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
LIST_OMIT_VALUES = frozenset({"--cargo-message-format", "--rerun", "-R"})


def build_selection(arguments: Sequence[str], *, for_list: bool = False) -> list[str]:
    """Keep selection options and the test arguments after `--`."""
    dropped_values = RUN_ONLY_VALUE_OPTIONS | LIST_OMIT_VALUES if for_list else RUN_ONLY_VALUE_OPTIONS
    kept: list[str] = []
    skip_value = False
    for index, argument in enumerate(arguments):
        if skip_value:
            skip_value = False
            continue
        if argument == "--":
            kept.extend(arguments[index:])
            break
        name = argument.partition("=")[0]
        if argument in RUN_ONLY_FLAGS or name in dropped_values and "=" in argument:
            continue
        if argument in dropped_values:
            skip_value = True
            continue
        if argument.startswith("-j") and argument[2:].isdecimal():
            continue
        if for_list and argument.startswith("-R"):
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

    def list_selected(self) -> list[str]:
        # The filter-matched count bounds rerun tests before the launcher applies the width limit.
        selection = build_selection(self.rest, for_list=True)
        return [*self.head[:2], "list", "--message-format", "json", *selection]

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


def exit_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def run_child(argv: Sequence[str], *, environment: Mapping[str, str] = os.environ) -> int:
    """Run one child in this process group, and end when it ends."""
    return exit_status(subprocess.run(argv, check=False, env=environment).returncode)


@dataclass(frozen=True)
class SelectedTestCount:
    tests: int


@dataclass(frozen=True)
class UnavailableTestCount:
    reason: str


def selected_test_count(run: SizedTestRun) -> SelectedTestCount | UnavailableTestCount:
    """Count tests whose nextest filter result is a match."""
    try:
        result = subprocess.run(run.list_selected(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
        summary = cast(object, json.loads(result.stdout))
    except subprocess.CalledProcessError as error:
        return UnavailableTestCount(f"nextest list exited {exit_status(error.returncode)}")
    except OSError:
        return UnavailableTestCount("could not start nextest list")
    except ValueError:
        return UnavailableTestCount("nextest list returned invalid JSON")

    invalid = UnavailableTestCount("nextest list returned an unrecognized test list")
    if not isinstance(summary, dict) or not isinstance(suites := cast(dict[str, object], summary).get("rust-suites"), dict):
        return invalid
    count = 0
    for suite in cast(dict[str, object], suites).values():
        if not isinstance(suite, dict) or not isinstance(cases := cast(dict[str, object], suite).get("testcases"), dict):
            return invalid
        for case in cast(dict[str, object], cases).values():
            match case:
                case {"filter-match": {"status": "matches"}}:
                    count += 1
                case {"filter-match": {"status": "mismatch"}}:
                    continue
                case _:
                    return invalid
    return SelectedTestCount(count)


def clean_jobserver_parent(launcher_start: process_start.ProcessStart) -> Path:
    """Remove folders that belong to launchers that ended."""
    temp_root = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).absolute()
    parent = temp_root / f"nextest-jobservers-{os.getuid()}"
    parent.mkdir(mode=0o700, exist_ok=True)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError(f"nextest: jobserver parent is not a private directory: {parent}")
    if not isinstance(launcher_start, process_start.Started):
        return parent
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                owner = re.fullmatch(r"([1-9][0-9]{0,9})-([0-9a-f]+|unknown)-[a-z0-9_]+", entry.name)
                if owner is None:
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    started = process_start.process_start(int(owner[1]))
                    if isinstance(started, process_start.Unreadable):
                        continue
                    if isinstance(started, process_start.Started) and owner[2] in ("unknown", started.start.encode().hex()):
                        continue
                    shutil.rmtree(entry.path)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    print(f"nextest: could not remove stale jobserver folder {entry.path}: {error}",
                          file=sys.stderr, flush=True)
    except OSError as error:
        print(f"nextest: could not scan jobserver folders in {parent}: {error}", file=sys.stderr, flush=True)
    return parent


def run_test_phase(run: SizedTestRun, threads: int, nested: bytearray, parent: Path,
                   launcher_start: process_start.ProcessStart) -> int:
    if not nested:
        return run_child(run.with_threads(threads))
    pid = os.getpid()
    start = launcher_start.start.encode().hex() if isinstance(launcher_start, process_start.Started) else "unknown"
    with tempfile.TemporaryDirectory(dir=parent, prefix=f"{pid}-{start}-") as directory:
        fifo = Path(directory) / "pool"
        os.mkfifo(fifo, 0o600)
        descriptor = os.open(fifo, os.O_RDWR | os.O_CLOEXEC)
        try:
            _ = os.write(descriptor, nested)
            if sys.platform == "linux":
                # Keep the FIFO reachable only while the holder owns the descriptor.
                fifo.unlink()
                fifo = Path(f"/proc/{pid}/fd/{descriptor}")
            return run_child(run.with_threads(threads), environment={
                **os.environ, "CARGO_MAKEFLAGS": f"--jobserver-auth=fifo:{fifo}",
            })
        finally:
            os.close(descriptor)


def record_reservation_width(reservation: str, threads: int) -> None:
    """Give the step's memory reservation the width its tests run at."""
    from scripts.build_queue import admission_client

    _ = admission_client.width(reservation, threads)


def thread_line(threads: int, width: int) -> str:
    line = budget.thread_line(budget.SpecifiedTestWidth(str(threads)), budget.SpecifiedTestWidth(str(width)))
    return f"{line}; one jobserver token for each thread"


def run_with_tokens(run: SizedTestRun, pool_path: Path, floor: int, width_file: Path | None,
                    record_width: Callable[[int], None], *, nested_tokens: int) -> int:
    """Build, count tests, and split the tokens between tests and nested Cargo."""
    launcher_start = process_start.process_start(os.getpid())
    parent = clean_jobserver_parent(launcher_start)
    status = run_child(run.build_only())
    if status != 0:
        return status
    selection = selected_test_count(run)
    limit = run.width
    test_floor = min(floor, run.width)
    if isinstance(selection, UnavailableTestCount):
        print(f"nextest: {selection.reason}; using requested width {limit} as the token limit", flush=True)
    elif selection.tests == 0:
        print("nextest: no selected tests; no jobserver tokens", flush=True)
        return run_child(run.with_threads(run.width))
    else:
        limit = min(limit, selection.tests + nested_tokens)
        test_floor = min(test_floor, selection.tests)
    try:
        pool = TokenPool(os.open(pool_path, os.O_RDWR | os.O_CLOEXEC))
    except OSError as error:
        print(f"nextest: {run.width} test threads with no jobserver tokens: {pool_path}: {error}", flush=True)
        return run_child(run.with_threads(run.width))
    try:
        pool.take_free(test_floor)
        if len(pool.held) < test_floor:
            # The Builds page reads this line as the token wait stage.
            print(progress_profile.token_wait_line(test_floor - len(pool.held)), flush=True)
            pool.take_waiting(test_floor)
        pool.take_free(limit)
        held = len(pool.held)
        nested = min(nested_tokens, held - test_floor)
        threads = held - nested
        if width_file is not None:
            _ = width_file.write_text(f"{threads}\n", encoding="utf-8")
        if threads != run.width:
            record_width(threads)
        print(thread_line(threads, run.width), flush=True)
        if nested:
            print(f"nextest: {nested} jobserver tokens for nested Cargo", flush=True)
        return run_test_phase(run, threads, pool.held[threads:], parent, launcher_start)
    finally:
        pool.give_back()


class Arguments(argparse.Namespace):
    pool: Path = Path()
    width_file: Path | None = None
    reservation: str = ""
    command: list[str] = []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hold one jobserver token for each nextest test thread.")
    _ = parser.add_argument("--pool", type=Path, required=True)
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

    run = test_run(command)
    if isinstance(run, UnsizedCommand):
        return run_child(run.argv)
    configuration = conf.read("build_admission")
    return run_with_tokens(run, pool_path, configuration.whole("test_token_floor"), width_file, record_width,
                           nested_tokens=configuration.whole("nested_tokens"))


if __name__ == "__main__":
    sys.exit(main())

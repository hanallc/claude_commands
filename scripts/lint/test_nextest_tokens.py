#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Nextest test threads hold jobserver tokens, from a fake pool in a temporary directory."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import IO, cast, override
from unittest import mock

from scripts.build_hold.override_guard import require_checkout
from scripts.lint import nextest_tokens as tokens

require_checkout(Path(__file__))


INVOKE = Path(__file__).with_name("invoke.sh")
HOLDER = Path(__file__).with_name("nextest_tokens.py")
PY = Path.home() / ".claude/scripts/lib/py"

# The fake cargo logs each call. A test run can end itself by a signal, or wait
# until the case stops it.
FAKE_CARGO = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CARGO_CALLS"
[[ -z "${FAKE_QUIET:-}" ]] || exec >/dev/null
if [[ " $* " == *" --no-run "* ]]; then
    echo built
    exit "${FAKE_BUILD_STATUS:-0}"
fi
case "${FAKE_TEST:-pass}" in
    killed) kill -KILL $$ ;;
    wait) echo "started $$"; exec sleep 60 ;;
esac
echo tested
"""


class PoolCase(unittest.TestCase):
    """A FIFO jobserver pool with a fixed number of tokens."""

    root: Path = Path()
    pool: Path = Path()
    calls: Path = Path()
    keeper: int = -1
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.pool = self.root / "pool"
        os.mkfifo(self.pool)
        # The case keeps one end open, so the pool keeps its tokens between runs.
        self.keeper = os.open(self.pool, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, self.keeper)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        cargo = bin_dir / "cargo"
        _ = cargo.write_text(FAKE_CARGO, encoding="utf-8")
        cargo.chmod(0o755)
        configuration = self.root / "config"
        configuration.mkdir()
        _ = (configuration / "build_admission.conf").write_text("[memory]\n# whole: 1..\ntest_token_floor=2\n",
                                                                encoding="utf-8")
        self.calls = self.root / "calls"
        self.environment = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                            "CARGO_CALLS": str(self.calls), "CONF_DIR": str(configuration)}

    def fill(self, count: int) -> None:
        _ = os.write(self.keeper, bytes(range(count)))

    def take(self, count: int) -> bytes:
        return os.read(self.keeper, count)

    def free_tokens(self) -> int:
        try:
            taken = os.read(self.keeper, 64)
        except BlockingIOError:
            return 0
        _ = os.write(self.keeper, taken)
        return len(taken)

    def holder(self, *command: str, extra: dict[str, str] | None = None) -> subprocess.Popen[str]:
        # Its own process group stands in for the step's scope.
        child = subprocess.Popen(
            [str(PY), str(HOLDER), "--pool", str(self.pool), "--width-file", str(self.root / "width"), "--",
             *command],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
            env={**self.environment, **(extra or {})}, start_new_session=True,
        )
        self.addCleanup(close_stream, child.stdout)
        self.addCleanup(stop, child)
        return child

    def cargo_calls(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines()


def close_stream(stream: IO[str] | None) -> None:
    if stream is not None:
        stream.close()


def stop(child: subprocess.Popen[str]) -> None:
    if child.poll() is None:
        child.kill()
        _ = child.wait()


def output_of(child: subprocess.Popen[str]) -> IO[str]:
    assert child.stdout is not None
    return cast(IO[str], child.stdout)


def read_line(child: subprocess.Popen[str]) -> str:
    return output_of(child).readline()


class HolderTests(PoolCase):
    def test_run_waits_for_its_floor_while_a_compile_holds_the_pool(self) -> None:
        self.fill(4)
        compile_tokens = self.take(3)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8", "--workspace")
        self.assertEqual(read_line(child), "built\n")
        self.assertEqual(read_line(child), "nextest: waiting for 1 more jobserver tokens\n")
        _ = os.write(self.keeper, compile_tokens[:1])
        self.assertRegex(read_line(child), r"\Anextest: 2 of 8 test threads; load .*; one jobserver token for each thread\n\Z")
        self.assertEqual(read_line(child), "tested\n")
        self.assertEqual(child.wait(timeout=30), 0)
        self.assertEqual(self.cargo_calls(), ["nextest run --no-run --workspace",
                                              "nextest run --test-threads 2 --workspace"])
        self.assertEqual((self.root / "width").read_text(encoding="utf-8"), "2\n")
        self.assertEqual(self.free_tokens(), 2)

    def test_free_tokens_up_to_the_width_and_every_token_goes_back(self) -> None:
        self.fill(12)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8", "-E", "test(one)", "--", "--nocapture")
        output = output_of(child).read()
        self.assertEqual(child.wait(timeout=30), 0, output)
        self.assertEqual(self.cargo_calls(), ["nextest run --no-run -E test(one)",
                                              "nextest run --test-threads 8 -E test(one) -- --nocapture"])
        self.assertEqual(self.free_tokens(), 12)

    def test_compile_failure_takes_no_token_and_runs_no_tests(self) -> None:
        self.fill(4)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8", extra={"FAKE_BUILD_STATUS": "101"})
        self.assertEqual(child.wait(timeout=30), 101)
        self.assertEqual(self.cargo_calls(), ["nextest run --no-run"])
        self.assertFalse((self.root / "width").exists())
        self.assertEqual(self.free_tokens(), 4)

    def test_killed_child_gives_every_token_back(self) -> None:
        self.fill(4)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8", extra={"FAKE_TEST": "killed"})
        self.assertEqual(child.wait(timeout=30), 128 + signal.SIGKILL)
        self.assertEqual(self.free_tokens(), 4)

    def test_a_signal_to_the_step_group_ends_the_holder_and_its_tests(self) -> None:
        # A FIFO pool cannot take back the tokens of an ended process; steve does.
        self.fill(4)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8", extra={"FAKE_TEST": "wait"})
        self.assertEqual(read_line(child), "built\n")
        _ = read_line(child)
        tests = os.pidfd_open(int(read_line(child).removeprefix("started ")))
        self.addCleanup(os.close, tests)
        self.assertEqual(self.free_tokens(), 0)
        os.killpg(child.pid, signal.SIGTERM)
        self.assertEqual(child.wait(timeout=30), -signal.SIGTERM)
        self.assertEqual(select.select([tests], [], [], 30)[0], [tests])

    def test_a_signal_while_the_run_waits_ends_it_before_its_tests(self) -> None:
        self.fill(1)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8")
        self.assertEqual(read_line(child), "built\n")
        self.assertEqual(read_line(child), "nextest: waiting for 1 more jobserver tokens\n")
        os.killpg(child.pid, signal.SIGTERM)
        self.assertEqual(child.wait(timeout=30), -signal.SIGTERM)
        self.assertEqual(self.cargo_calls(), ["nextest run --no-run"])

    def test_other_commands_run_as_given_with_no_token(self) -> None:
        self.fill(4)
        for command in (("cargo", "nextest", "run", "--no-run", "--test-threads", "8"),
                        ("cargo", "nextest", "run", "--test-threads", "0"),
                        ("cargo", "nextest", "run", "--workspace")):
            with self.subTest(command=command):
                self.calls.unlink(missing_ok=True)
                child = self.holder(*command)
                self.assertEqual(child.wait(timeout=30), 0)
                self.assertEqual(self.cargo_calls(), [" ".join(command[1:])])
                self.assertEqual(self.free_tokens(), 4)


class ReservationTests(PoolCase):
    def test_reservation_width_is_the_tokens_held(self) -> None:
        self.fill(3)
        recorded: list[int] = []
        run = tokens.test_run(["cargo", "nextest", "run", "--test-threads", "8", "--workspace"])
        assert isinstance(run, tokens.SizedTestRun)
        with mock.patch.dict(os.environ, {**self.environment, "FAKE_QUIET": "1"}), \
                mock.patch("sys.stdout", new=open(os.devnull, "w")) as quiet:
            self.addCleanup(quiet.close)
            status = tokens.run_with_tokens(run, self.pool, 2, self.root / "width", recorded.append)
        self.assertEqual(status, 0)
        self.assertEqual(recorded, [3])
        self.assertEqual((self.root / "width").read_text(encoding="utf-8"), "3\n")
        self.assertEqual(self.cargo_calls()[-1], "nextest run --test-threads 3 --workspace")

    def test_full_width_needs_no_reservation_change(self) -> None:
        self.fill(8)
        recorded: list[int] = []
        run = tokens.test_run(["cargo", "nextest", "run", "--test-threads", "8"])
        assert isinstance(run, tokens.SizedTestRun)
        with mock.patch.dict(os.environ, {**self.environment, "FAKE_QUIET": "1"}), \
                mock.patch("sys.stdout", new=open(os.devnull, "w")) as quiet:
            self.addCleanup(quiet.close)
            self.assertEqual(tokens.run_with_tokens(run, self.pool, 2, None, recorded.append), 0)
        self.assertEqual(recorded, [])


class InvokeTests(PoolCase):
    """run_nextest through the real run_once, with the memory gate and build log stubbed."""

    def run_nextest(self, makeflags: str, *arguments: str) -> str:
        config_reader = self.root / "lint_config.sh"
        _ = config_reader.write_text("lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n")
        script = """
set -euo pipefail
source "$1"
shift
require_nextest() { :; }
buildlog_step_compiles() { return 1; }
buildlog_finish_memory() { :; }
buildlog_end() { shift 3; printf 'recorded:%s\\n' "$*"; }
nextest_logical_cores() { LINT_LOGICAL_CORES=8; }
LINT_TEST_WIDTH=logical
run_nextest "$@"
"""
        environment = {
            **self.environment,
            "CARGO_MAKEFLAGS": makeflags,
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_OFF": "1", "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "", "BUILDLOG_SCOPE": "0",
            "LINT_SKIP_SWEEP": "1", "TMPDIR": str(self.root),
            "BUILD_HOLD_DIR": str(self.root / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "LINT_CONFIG_READER": str(config_reader),
        }
        result = subprocess.run(["bash", "-c", script, "nextest-tokens-test", str(INVOKE), *arguments],
                                capture_output=True, text=True, check=False, env=environment,
                                stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_pool_run_builds_first_and_records_the_width_it_ran_at(self) -> None:
        self.fill(5)
        output = self.run_nextest(f"-j --jobserver-auth=fifo:{self.pool}", "--workspace")
        self.assertRegex(output, r"(?s)\A\+ cargo nextest run --test-threads 8 --workspace\nbuilt\n"
                         + r"nextest: 5 of 8 test threads; [^\n]*; one jobserver token for each thread\ntested\n"
                         + r"recorded:cargo nextest run --test-threads 5 --workspace\n\Z")
        self.assertEqual(self.cargo_calls(), ["nextest run --no-run --workspace",
                                              "nextest run --test-threads 5 --workspace"])
        self.assertEqual(self.free_tokens(), 5)
        self.assertEqual([path.name for path in self.root.glob("nextest_tokens.*")], [])

    def test_no_pool_keeps_the_mode_width_and_takes_no_token(self) -> None:
        self.fill(5)
        for makeflags in (f"--jobserver-auth=fifo:{self.root / 'missing'}", "-j4", f"--jobserver-auth={self.pool}"):
            with self.subTest(makeflags=makeflags):
                self.calls.unlink(missing_ok=True)
                output = self.run_nextest(makeflags, "--workspace")
                self.assertRegex(output, r"\A\+ cargo nextest run --test-threads 8 --workspace\n"
                                 + r"nextest: 8 of 8 test threads; [^\n]*\(.*\)\ntested\n"
                                 + r"recorded:cargo nextest run --test-threads 8 --workspace\n\Z")
                self.assertEqual(self.cargo_calls(), ["nextest run --test-threads 8 --workspace"])
                self.assertEqual(self.free_tokens(), 5)


if __name__ == "__main__":
    _ = unittest.main(argv=sys.argv)

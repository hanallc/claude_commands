#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Nextest test threads hold jobserver tokens, from a fake pool in a temporary directory."""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import IO, cast, override
from unittest import mock

from scripts.build_hold.override_guard import require_checkout
from scripts.lib.process_group import send_process_group
from scripts.lib.process_start import Started, process_start
from scripts.lint import nextest_tokens as tokens
from scripts.lint.memory_test_support import memory_gate_shell_environment

require_checkout(Path(__file__))


INVOKE = Path(__file__).with_name("invoke.sh")
HOLDER = Path(__file__).with_name("nextest_tokens.py")
# The repository's own launcher: the gate box has no installed ~/.claude copy.
PY = Path(__file__).resolve().parents[1] / "lib/py"

# The fake cargo logs each call. A test run can end itself by a signal, or wait
# until the case stops it.
FAKE_CARGO = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CARGO_CALLS"
if [[ $1 == nextest && $2 == list ]]; then
    for argument in "${@:3}"; do
        case "$argument" in
            --) break ;;
            --cargo-message-format|--cargo-message-format=*) echo '{"reason":"compiler-artifact"}' ;;
            --rerun|--rerun=*|-R*) exit 2 ;;
        esac
    done
    printf '%s\\n' "$FAKE_SELECTION"
    exit "${FAKE_LIST_STATUS:-0}"
fi
[[ -z "${FAKE_QUIET:-}" ]] || exec >/dev/null
if [[ " $* " == *" --no-run "* ]]; then
    if [[ -n "${FAKE_BUILD_ENV:-}" ]]; then
        printf '%s\\n' "$CARGO_MAKEFLAGS" > "$FAKE_BUILD_ENV"
    fi
    # nextest 0.9.143 refuses these with --no-run.
    for flag in --fail-fast --no-fail-fast --max-fail --debugger --tracer; do
        if [[ " $* " == *" $flag "* ]]; then
            echo "error: the argument '--no-run' cannot be used with '$flag'"
            exit 2
        fi
    done
    echo built
    exit "${FAKE_BUILD_STATUS:-0}"
fi
case "${FAKE_TEST:-pass}" in
    killed) kill -KILL $$ ;;
    wait) echo "started $$"; exec sleep 60 ;;
    inspect) python3 - "$FAKE_TOKEN_POOL" <<'PY'
import array
import fcntl
import os
import sys
import termios

with open(sys.argv[1], "r+b", buffering=0) as pool:
    count = array.array("i", [0])
    fcntl.ioctl(pool, termios.FIONREAD, count, True)
    print(f"free during nextest: {count[0]}")
if os.environ.get("FAKE_BORROW"):
    path = os.environ["CARGO_MAKEFLAGS"].removeprefix("--jobserver-auth=fifo:")
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        try:
            tokens = os.read(fd, 64)
        except BlockingIOError:
            tokens = b""
        print(f"nested pool: {path}")
        print(f"nested borrowed: {len(tokens)}")
        os.write(fd, tokens)
    finally:
        os.close(fd)
PY
    ;;
esac
echo tested
"""


def fake_selection(count: int) -> str:
    cases = {f"test_{index}": {"filter-match": {"status": "matches"}} for index in range(count)}
    cases["ignored"] = {"filter-match": {"status": "mismatch", "reason": "ignored"}}
    return json.dumps({"test-count": len(cases), "rust-suites": {"fixture": {"testcases": cases}}})


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
        self.configure_nested_tokens(0)
        self.calls = self.root / "calls"
        self.environment = {**memory_gate_shell_environment(),
                            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                            "CARGO_CALLS": str(self.calls), "CONF_DIR": str(configuration),
                            "FAKE_SELECTION": fake_selection(16), "FAKE_TOKEN_POOL": str(self.pool),
                            "TMPDIR": str(self.root)}

    def configure_nested_tokens(self, count: int, *, test_floor: int = 2) -> None:
        _ = (self.root / "config/build_admission.conf").write_text(
            f"[memory]\n# whole: 1..\ntest_token_floor={test_floor}\n# whole: 0..\nnested_tokens={count}\n",
            encoding="utf-8",
        )

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


# The shape verify.sh passes for a hana test step.
HANA_RUN = ("cargo", "nextest", "run", "--test-threads", "32", "--no-fail-fast", "--workspace", "--bin", "hana",
            "-E", "package(hana) & test(camera_output_cable_ink_reaches_brightness_source_from_the_face)",
            "--features", "hana/dynamic_linking")


class BuildOnlyTests(unittest.TestCase):
    def test_the_build_keeps_the_selection_and_drops_every_run_only_option(self) -> None:
        run = tokens.test_run(HANA_RUN)
        assert isinstance(run, tokens.SizedTestRun)
        self.assertEqual(run.build_only(), ["cargo", "nextest", "run", "--no-run", "--workspace", "--bin", "hana",
            "-E", "package(hana) & test(camera_output_cable_ink_reaches_brightness_source_from_the_face)",
            "--features", "hana/dynamic_linking"])
        self.assertEqual(run.with_threads(8)[3:6], ["--test-threads", "8", "--no-fail-fast"])

    def test_each_run_only_form_is_dropped_with_its_value(self) -> None:
        for given in (["--fail-fast"], ["--no-fail-fast"], ["--max-fail", "3"], ["--max-fail=3"],
                      ["--retries", "2"], ["--retries=2"], ["-j", "4"], ["-j4"], ["--test-threads", "4"],
                      ["--status-level", "all"], ["--final-status-level=fail"], ["--success-output", "never"],
                      ["--failure-output", "final"], ["--no-capture"], ["--nocapture"], ["--debugger", "gdb"],
                      ["--tracer", "strace"], ["--no-tests", "pass"], ["--stress-count", "2"],
                      ["--message-format", "libtest-json"], ["--show-progress", "none"], ["--hide-progress-bar"]):
            with self.subTest(given=given):
                self.assertEqual(tokens.build_selection([*given, "--workspace", "-E", "all()"]),
                                 ["--workspace", "-E", "all()"])

    def test_build_options_and_filters_stay(self) -> None:
        kept = ["-p", "hana", "--lib", "--features=x", "--build-jobs", "4", "-P", "ci", "--release",
                "--cargo-profile", "test", "--partition", "count:1/2", "--run-ignored", "all", "-E", "all()"]
        self.assertEqual(tokens.build_selection(kept), kept)


class HolderTests(PoolCase):
    def test_one_selected_test_leaves_the_other_tokens_free(self) -> None:
        self.fill(8)
        for nested, free in ((0, 7), (3, 4)):
            with self.subTest(nested=nested):
                self.configure_nested_tokens(nested)
                options = {"FAKE_TEST": "inspect", "FAKE_SELECTION": fake_selection(1)}
                if nested:
                    options["FAKE_BORROW"] = "1"
                    options["CARGO_MAKEFLAGS"] = f"--jobserver-auth=fifo:{self.pool}"
                child = self.holder("cargo", "nextest", "run", "--test-threads", "8", "-E", "test(one)", extra=options)
                output, _ = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, output)
                self.assertIn(f"free during nextest: {free}\n", output)
                if nested:
                    self.assertIn("nested borrowed: 3\n", output)
                    self.assertNotIn(f"nested pool: {self.pool}\n", output)
                self.assertEqual((self.root / "width").read_text(encoding="utf-8"), "1\n")
                self.assertEqual(self.free_tokens(), 8)

    def test_full_suite_leaves_tokens_for_nested_cargo(self) -> None:
        self.fill(8)
        self.configure_nested_tokens(3)
        makeflags = f"--jobserver-auth=fifo:{self.pool}"
        build_environment = self.root / "build-environment"
        child = self.holder(
            "cargo", "nextest", "run", "--test-threads", "8", "--workspace",
            extra={"FAKE_TEST": "inspect", "FAKE_BORROW": "1", "CARGO_MAKEFLAGS": makeflags,
                   "FAKE_BUILD_ENV": str(build_environment)},
        )
        output, _ = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, output)
        self.assertIn("nested borrowed: 3\n", output)
        self.assertNotIn(f"nested pool: {self.pool}\n", output)
        self.assertIn("free during nextest: 0\n", output)
        self.assertEqual(build_environment.read_text(encoding="utf-8"), f"{makeflags}\n")
        self.assertEqual((self.root / "width").read_text(encoding="utf-8"), "5\n")
        self.assertEqual(self.free_tokens(), 8)
        self.assertEqual(list(self.root.glob("nextest-jobserver-*")), [])
        self.assertEqual(list((self.root / f"nextest-jobservers-{os.getuid()}").iterdir()), [])

    def test_empty_selection_uses_no_tokens_and_preserves_nextest_status(self) -> None:
        self.fill(4)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8", "--no-tests", "pass",
                            extra={"FAKE_TEST": "inspect", "FAKE_SELECTION": fake_selection(0)})
        output, _ = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, output)
        self.assertIn("free during nextest: 4\n", output)
        self.assertFalse((self.root / "width").exists())
        self.assertEqual(self.cargo_calls(), ["nextest run --no-run", "nextest list --message-format json",
                                              "nextest run --test-threads 8 --no-tests pass"])

    def test_unreadable_selection_uses_the_requested_token_limit(self) -> None:
        self.fill(8)
        for fixture in ({"FAKE_SELECTION": "invalid JSON"}, {"FAKE_LIST_STATUS": "1"}):
            with self.subTest(fixture=fixture):
                child = self.holder("cargo", "nextest", "run", "--test-threads", "8",
                                    extra={"FAKE_TEST": "inspect", **fixture})
                output, _ = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, output)
                self.assertEqual(output.count("using requested width 8 as the token limit"), 1, output)
                self.assertIn("free during nextest: 0\n", output)
                self.assertEqual(self.free_tokens(), 8)

    def test_list_counts_tests_with_run_options_that_change_listing(self) -> None:
        self.fill(8)
        for options in (("--cargo-message-format", "json"), ("--cargo-message-format=json",),
                        ("--rerun", "latest"), ("--rerun=latest",), ("-R", "latest"), ("-Rlatest",)):
            with self.subTest(options=options):
                self.calls.unlink(missing_ok=True)
                child = self.holder("cargo", "nextest", "run", "--test-threads", "8", *options, "--workspace",
                                    extra={"FAKE_TEST": "inspect", "FAKE_SELECTION": fake_selection(2)})
                output, _ = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, output)
                self.assertIn("free during nextest: 6\n", output)
                self.assertEqual((self.root / "width").read_text(encoding="utf-8"), "2\n")
                self.assertEqual(self.cargo_calls()[1:], ["nextest list --message-format json --workspace",
                    " ".join(("nextest run --test-threads 2", *options, "--workspace"))])
                self.assertEqual(self.free_tokens(), 8)

    def test_start_removes_folders_for_dead_owners_and_reused_pids(self) -> None:
        parent = self.root / f"nextest-jobservers-{os.getuid()}"
        parent.mkdir(mode=0o700)
        started = process_start(os.getpid())
        assert isinstance(started, Started)
        live = parent / f"{os.getpid()}-{started.start.encode().hex()}-live"
        reused = parent / f"{os.getpid()}-{(started.start + '0').encode().hex()}-reused"
        with subprocess.Popen([sys.executable, "-c", "pass"]) as owner:
            self.assertEqual(owner.wait(timeout=10), 0)
            dead = parent / f"{owner.pid}-{b'0'.hex()}-dead"
        for folder in (live, reused, dead):
            folder.mkdir(mode=0o700)
            os.mkfifo(folder / "pool")
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8",
                            extra={"FAKE_SELECTION": fake_selection(0)})
        output, _ = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, output)
        self.assertEqual(sorted(path.name for path in parent.iterdir()), [live.name])
        self.assertTrue((live / "pool").is_fifo())

    @unittest.skipIf(sys.platform == "darwin", "macOS uses ps for process start times.")
    def test_missing_process_root_keeps_a_live_owners_folder(self) -> None:
        started = process_start(os.getpid())
        assert isinstance(started, Started)
        parent = self.root / f"nextest-jobservers-{os.getuid()}"
        parent.mkdir(mode=0o700)
        live = parent / f"{os.getpid()}-{started.start.encode().hex()}-live"
        live.mkdir(mode=0o700)
        os.mkfifo(live / "pool")
        self.fill(8)
        self.configure_nested_tokens(3)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8",
                            extra={"VERIFY_PROC_ROOT": str(self.root / "missing-proc"),
                                   "FAKE_TEST": "inspect", "FAKE_BORROW": "1"})
        output, _ = child.communicate(timeout=30)
        self.assertTrue((live / "pool").is_fifo(), output)
        self.assertEqual(child.returncode, 0, output)
        self.assertIn("nested borrowed: 3\n", output)
        self.assertEqual(sorted(parent.iterdir()), [live])
        self.assertEqual(self.free_tokens(), 8)

    def test_cleanup_error_reports_the_folder_and_the_tests_run(self) -> None:
        started = process_start(os.getpid())
        assert isinstance(started, Started)
        parent = self.root / f"nextest-jobservers-{os.getuid()}"
        parent.mkdir(mode=0o700)
        stale = parent / f"{os.getpid()}-{(started.start + '0').encode().hex()}-stale"
        stale.mkdir(mode=0o700)
        self.fill(4)
        run = tokens.test_run(["cargo", "nextest", "run", "--test-threads", "4"])
        assert isinstance(run, tokens.SizedTestRun)
        errors = StringIO()
        remove = mock.Mock(side_effect=PermissionError("fixture denies removal"))
        with (mock.patch.dict(os.environ, {**self.environment, "FAKE_QUIET": "1"}),
              mock.patch.object(tokens, "shutil", SimpleNamespace(rmtree=remove)),
              mock.patch("sys.stderr", errors)):
            status = tokens.run_with_tokens(run, self.pool, 2, None, lambda _: None, nested_tokens=0)
        remove.assert_called_once_with(str(stale))
        self.assertEqual(errors.getvalue().splitlines(), [
            f"nextest: could not remove stale jobserver folder {stale}: fixture denies removal",
        ])
        self.assertEqual(status, 0)
        self.assertEqual(self.cargo_calls()[-1], "nextest run --test-threads 4")
        self.assertTrue(stale.is_dir())
        self.assertEqual(self.free_tokens(), 4)

    def test_cleanup_does_not_report_a_folder_that_another_launcher_removed(self) -> None:
        started = process_start(os.getpid())
        assert isinstance(started, Started)
        parent = self.root / f"nextest-jobservers-{os.getuid()}"
        parent.mkdir(mode=0o700)
        stale = parent / f"{os.getpid()}-{(started.start + '0').encode().hex()}-stale"
        stale.mkdir(mode=0o700)

        def remove_first(path: str) -> None:
            Path(path).rmdir()
            raise FileNotFoundError(path)

        errors = StringIO()
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(shutil, "rmtree", side_effect=remove_first) as remove,
              mock.patch("sys.stderr", errors)):
            self.assertEqual(tokens.clean_jobserver_parent(started), parent)
        remove.assert_called_once_with(str(stale))
        self.assertFalse(stale.exists())
        self.assertEqual(errors.getvalue(), "")

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
                                              "nextest list --message-format json --workspace",
                                              "nextest run --test-threads 2 --workspace"])
        self.assertEqual((self.root / "width").read_text(encoding="utf-8"), "2\n")
        self.assertEqual(self.free_tokens(), 2)

    def test_thread_floor_takes_priority_over_nested_cargo_at_the_width_limit(self) -> None:
        self.fill(8)
        compile_tokens = self.take(1)
        self.configure_nested_tokens(3, test_floor=4)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "4", extra={"FAKE_TEST": "inspect"})
        output, _ = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, output)
        self.assertEqual(self.cargo_calls()[-1], "nextest run --test-threads 4")
        self.assertIn("free during nextest: 3\n", output)
        self.assertEqual(self.free_tokens(), 7)
        _ = os.write(self.keeper, compile_tokens)

    def test_nested_cargo_cannot_borrow_shared_tokens_at_the_width_limit(self) -> None:
        self.fill(7)
        self.configure_nested_tokens(3, test_floor=4)
        child = self.holder(
            "cargo", "nextest", "run", "--test-threads", "4",
            extra={"FAKE_TEST": "inspect", "FAKE_BORROW": "1", "CARGO_MAKEFLAGS": f"--jobserver-auth=fifo:{self.pool}"},
        )
        output, _ = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, output)
        self.assertIn("free during nextest: 3\n", output)
        self.assertIn("nested borrowed: 0\n", output)
        self.assertEqual(self.free_tokens(), 7)

    def test_the_test_floor_can_use_the_entire_pool(self) -> None:
        self.fill(4)
        self.configure_nested_tokens(3, test_floor=4)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8", extra={"FAKE_TEST": "inspect"})
        output, _ = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, output)
        self.assertEqual(self.cargo_calls()[-1], "nextest run --test-threads 4")
        self.assertNotIn("waiting for", output)
        self.assertNotIn("tokens for nested Cargo", output)
        self.assertIn("free during nextest: 0\n", output)
        self.assertEqual(self.free_tokens(), 4)

    def test_run_waits_for_the_test_floor_and_gives_cargo_the_surplus(self) -> None:
        self.fill(6)
        compile_tokens = self.take(3)
        self.configure_nested_tokens(3, test_floor=4)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8",
                            extra={"FAKE_TEST": "inspect", "FAKE_BORROW": "1"})
        self.assertEqual(read_line(child), "built\n")
        self.assertEqual(read_line(child), "nextest: waiting for 1 more jobserver tokens\n")
        _ = os.write(self.keeper, compile_tokens[:2])
        output, _ = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, output)
        self.assertEqual(self.cargo_calls()[-1], "nextest run --test-threads 4")
        self.assertIn("nested borrowed: 1\n", output)
        self.assertEqual(self.free_tokens(), 5)

    def test_two_selected_tests_limit_the_test_floor_to_two(self) -> None:
        self.fill(5)
        compile_tokens = self.take(4)
        self.configure_nested_tokens(3, test_floor=4)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8",
                            extra={"FAKE_TEST": "inspect", "FAKE_BORROW": "1", "FAKE_SELECTION": fake_selection(2)})
        self.assertEqual(read_line(child), "built\n")
        self.assertEqual(read_line(child), "nextest: waiting for 1 more jobserver tokens\n")
        _ = os.write(self.keeper, compile_tokens)
        output, _ = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, output)
        self.assertEqual(self.cargo_calls()[-1], "nextest run --test-threads 2")
        self.assertIn("nested borrowed: 3\n", output)
        self.assertEqual(self.free_tokens(), 5)

    def test_free_tokens_up_to_the_width_and_every_token_goes_back(self) -> None:
        self.fill(12)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8", "-E", "test(one)", "--", "--nocapture")
        output = output_of(child).read()
        self.assertEqual(child.wait(timeout=30), 0, output)
        self.assertEqual(self.cargo_calls(), ["nextest run --no-run -E test(one)",
                                              "nextest list --message-format json -E test(one) -- --nocapture",
                                              "nextest run --test-threads 8 -E test(one) -- --nocapture"])
        self.assertEqual(self.free_tokens(), 12)

    def test_compile_failure_takes_no_token_and_runs_no_tests(self) -> None:
        self.fill(4)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8", extra={"FAKE_BUILD_STATUS": "101"})
        self.assertEqual(child.wait(timeout=30), 101)
        self.assertEqual(self.cargo_calls(), ["nextest run --no-run"])
        self.assertFalse((self.root / "width").exists())
        self.assertEqual(self.free_tokens(), 4)

    def test_a_hana_run_builds_without_its_run_only_flags_then_runs_with_them(self) -> None:
        self.fill(4)
        child = self.holder(*HANA_RUN)
        output = output_of(child).read()
        self.assertEqual(child.wait(timeout=30), 0, output)
        selection = " ".join(HANA_RUN[6:])
        self.assertEqual(self.cargo_calls(), [f"nextest run --no-run {selection}",
                                              f"nextest list --message-format json {selection}",
                                              f"nextest run --test-threads 4 --no-fail-fast {selection}"])
        self.assertEqual(self.free_tokens(), 4)

    def test_killed_child_gives_every_token_back(self) -> None:
        self.fill(4)
        self.configure_nested_tokens(3)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8", extra={"FAKE_TEST": "killed"})
        self.assertEqual(child.wait(timeout=30), 128 + signal.SIGKILL)
        self.assertEqual(self.free_tokens(), 4)
        self.assertEqual(list(self.root.glob("nextest-jobserver-*")), [])
        self.assertEqual(list((self.root / f"nextest-jobservers-{os.getuid()}").iterdir()), [])

    def test_a_signal_to_the_step_group_ends_the_holder_and_its_tests(self) -> None:
        # A FIFO pool cannot take back the tokens of an ended process; steve does.
        self.fill(4)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8", extra={"FAKE_TEST": "wait"})
        self.assertEqual(read_line(child), "built\n")
        _ = read_line(child)
        tests = os.pidfd_open(int(read_line(child).removeprefix("started ")))
        self.addCleanup(os.close, tests)
        self.assertEqual(self.free_tokens(), 0)
        send_process_group(child.pid, signal.SIGTERM)
        self.assertEqual(child.wait(timeout=30), -signal.SIGTERM)
        self.assertEqual(select.select([tests], [], [], 30)[0], [tests])

    def test_a_signal_while_the_run_waits_ends_it_before_its_tests(self) -> None:
        self.fill(1)
        child = self.holder("cargo", "nextest", "run", "--test-threads", "8")
        self.assertEqual(read_line(child), "built\n")
        self.assertEqual(read_line(child), "nextest: waiting for 1 more jobserver tokens\n")
        send_process_group(child.pid, signal.SIGTERM)
        self.assertEqual(child.wait(timeout=30), -signal.SIGTERM)
        self.assertEqual(self.cargo_calls(), ["nextest run --no-run", "nextest list --message-format json"])

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
            status = tokens.run_with_tokens(run, self.pool, 2, self.root / "width", recorded.append, nested_tokens=0)
        self.assertEqual(status, 0)
        self.assertEqual(recorded, [3])
        self.assertEqual((self.root / "width").read_text(encoding="utf-8"), "3\n")
        self.assertEqual(self.cargo_calls()[-1], "nextest run --test-threads 3 --workspace")

    def test_reservation_width_records_one_test_thread_with_one_nested_token(self) -> None:
        self.fill(2)
        recorded: list[int] = []
        run = tokens.test_run(["cargo", "nextest", "run", "--test-threads", "8", "--workspace"])
        assert isinstance(run, tokens.SizedTestRun)
        with mock.patch.dict(os.environ, {**self.environment, "FAKE_QUIET": "1"}), \
                mock.patch("sys.stdout", new=open(os.devnull, "w")) as quiet:
            self.addCleanup(quiet.close)
            status = tokens.run_with_tokens(run, self.pool, 1, self.root / "width", recorded.append, nested_tokens=3)
        self.assertEqual(status, 0)
        self.assertEqual(recorded, [1])
        self.assertEqual((self.root / "width").read_text(encoding="utf-8"), "1\n")
        self.assertEqual(self.cargo_calls()[-1], "nextest run --test-threads 1 --workspace")
        self.assertEqual(self.free_tokens(), 2)

    def test_full_width_needs_no_reservation_change(self) -> None:
        self.fill(8)
        recorded: list[int] = []
        run = tokens.test_run(["cargo", "nextest", "run", "--test-threads", "8"])
        assert isinstance(run, tokens.SizedTestRun)
        with mock.patch.dict(os.environ, {**self.environment, "FAKE_QUIET": "1"}), \
                mock.patch("sys.stdout", new=open(os.devnull, "w")) as quiet:
            self.addCleanup(quiet.close)
            self.assertEqual(tokens.run_with_tokens(run, self.pool, 2, None, recorded.append, nested_tokens=0), 0)
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
                                              "nextest list --message-format json --workspace",
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

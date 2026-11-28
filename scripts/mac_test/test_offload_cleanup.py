"""Offload remote results, cleanup and shell protocols."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
import io
from pathlib import Path
from typing import Literal, cast, override
from unittest import mock
from scripts.mac_test import eligibility, floor_claims, offload, mac_test, nextest_log
from scripts.mac_test.mirror_root import MacMirrorRoot
from scripts.mac_test.offload_test_commands import (
    ACTIVITY_SELECTOR_STUB, install_nextest_log_writer, write_shell,
)
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE, UNREACHABLE_WAIT_FIXTURE_SECONDS
from scripts.mac_test.offload_test_support import HANA, HANA_COMMAND_PATTERN, SelectorEvent
from scripts.lib.checkout_test_support import fixture_shim_source
from scripts.lib import build_folders, conf, removal_guard, test_hang_guard
from scripts.lib.shell_test_support import fixture_zsh_environment


MAC_TARGET_CONFIG = "targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n" + UNREACHABLE_WAIT_FIXTURE
DOC_INDEX_MIB = "250"
LINT_TRIM_CONFIG = f"""\
# range: 0..
recent_minutes=60
# range: 0..
sweep_doc_index_mib={DOC_INDEX_MIB}
# whole: 1..
sweep_max_tree_depth=3
# whole: 1..
sweep_floor_search_depth=8
sweep_floor_skip=.git node_modules
# whole: 1..
sweep_discovery_workers=4
# whole: 1..
sweep_dep_info_head_bytes=4096
"""

TRIM_FIXTURE = """\
import json
import os
import sys
from pathlib import Path
from unittest import mock
from scripts.lib import conf_reads, removal_guard
from scripts.lint import sweep, tmp_sweep

fixture = Path(os.environ["CLEANUP_TEST_DIR"])
keys = ("GIT_DIR", "GIT_WORK_TREE", "LINT_SWEEP_STATE_DIR", "LINT_SWEEP_DOC_INDEX_MIB")
(fixture / "trim-environment.json").write_text(json.dumps({key: os.environ[key] for key in keys if key in os.environ}))
removed_paths = []
remove = removal_guard.remove

def guarded_remove(checked, *, account):
    removed_paths.append(str(checked.path))
    if os.environ["CLEANUP_TEST_TRIM"] == "refuse":
        raise removal_guard.Refused(removal_guard.RefusalReason.MOUNT)
    remove(checked, account=account)

with (conf_reads.record_reads(fixture / "trim-config-reads"),
      mock.patch.object(tmp_sweep, "lsof_paths", return_value=tmp_sweep.ProcessUsesKnown([])),
      mock.patch.object(sweep, "mac_build_process_use", return_value=sweep.NoBuildProcessUse()),
      mock.patch.object(removal_guard, "remove", side_effect=guarded_remove)):
    try:
        exec(sys.argv.pop(1))
    finally:
        (fixture / "guarded-paths.json").write_text(json.dumps(removed_paths))
"""


def shell_environment(root: Path) -> dict[str, str]:
    """Give the test shells only the settings they need."""
    return {
        **fixture_zsh_environment(),
        "CONF_DIR": str(root / "config"),
        "HOME": str(root / "home with spaces"),
        "TMPDIR": str(root / "tmp"),
        "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib/python_startup"),
        "PYTHONPYCACHEPREFIX": os.environ.get("PYTHONPYCACHEPREFIX", str(root / "pycache")),
        "LINT_SWEEP_STATE_DIR": str(root / "sweep-state"),
        "BUILDLOG_DIR": str(root / "buildlog"),
        "LINT_SWEEP_DOC_INDEX_MIB": DOC_INDEX_MIB,
        "USER_ZONE": "UTC",
    }


def nextest_settings(tail_bytes: str = "65536", summary_bytes: str = "1024") -> str:
    return f"""\
# whole: 1..
{nextest_log.TAIL_BYTES}={tail_bytes}
# whole: 1..1024
{nextest_log.SUMMARY_BYTES}={summary_bytes}
"""


class MacCleanupProtocolTests(unittest.TestCase):
    def stream_protocol(self, controller: offload.ProcessController, arguments: list[str]) -> offload.RemoteRunStatus:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, shell_environment(Path(directory)), clear=True):
                return controller.stream_mac_run(arguments)

    def test_status_marker_followed_by_ordinary_output_is_printed_and_not_a_result(self) -> None:
        controller = offload.ProcessController()
        for marker in ("mac_exit=0", "mac_nextest_setup=failed"):
            with self.subTest(marker=marker):
                text = f"{marker}\ntest still running\n"
                output = io.StringIO()
                with redirect_stdout(output):
                    answer = self.stream_protocol(controller, [
                        "/bin/sh", "-c", 'printf "%s" "$1"; exit 255', "protocol", text,
                    ])
                self.assertIsInstance(answer, offload.MissingMacStatus)
                self.assertEqual(output.getvalue(), text)

    def test_trim_detail_cannot_turn_a_missing_verdict_into_a_mirror_refusal(self) -> None:
        controller = offload.ProcessController()
        text = ("mac_trim=status=1 detail=trim failed; "
                "copies: fixture-mirror: busy; held by session holder (mirror lease)\n"
                "mac_cleanup=1\n")
        output = io.StringIO()
        with redirect_stdout(output):
            answer = self.stream_protocol(controller, ["/bin/sh", "-c", 'printf "%s" "$1"', "protocol", text])
        self.assertIsInstance(answer, offload.MissingMacStatus)
        self.assertIsInstance(controller.pending, offload.PendingStartOperation)
        self.assertEqual(output.getvalue(), text.splitlines(keepends=True)[0])

    def test_stream_returns_cleanup_outcome_beside_the_reported_test_status(self) -> None:
        cases = (
            ("mac_cleanup=0 32715571 92\n", 0, offload.CleanupDone(offload.MeasuredCleanupSize(32715571), 92)),
            ("mac_cleanup=0 - 92.5\n", 0, offload.CleanupDone(offload.CleanupSizeNotMeasured(), 92.5)),
            ("mac_cleanup=kept\n", 0, offload.CleanupKept()),
            ("mac_cleanup=7\n", 0, offload.CleanupFailed(7)),
            ("mac_cleanup=skipped\n", 0, offload.CleanupSkipped()),
            ("mac_trim=status=1 elapsed_seconds=2 over_budget=yes detail=output locks are busy\nmac_cleanup=1\n",
             0, offload.CleanupFailed(1)),
            ("", 255, offload.CleanupUnconfirmed()),
        )
        for marker, ssh_status, expected in cases:
            with self.subTest(marker=marker, ssh_status=ssh_status):
                output = io.StringIO()
                controller = offload.ProcessController()
                with redirect_stdout(output):
                    answer = self.stream_protocol(controller, [
                        "/bin/sh", "-c", 'printf "%s" "$1"; exit "$2"', "protocol",
                        "test output\nmac_exit=42\n" + marker, str(ssh_status),
                    ])
                self.assertEqual(answer, offload.ReportedMacStatus(42, expected))
                trim_output = marker.splitlines(keepends=True)[0] if marker.startswith("mac_trim=") else ""
                self.assertEqual(output.getvalue(), "test output\n" + trim_output)

    def test_bare_success_confirms_cleanup_without_a_removal_line(self) -> None:
        output = io.StringIO()
        controller = offload.ProcessController()
        with redirect_stdout(output):
            answer = self.stream_protocol(controller, [
                "/bin/sh", "-c", 'printf "mac_exit=0\\nmac_cleanup=0\\n"',
            ])
            if not isinstance(answer, offload.ReportedMacStatus):
                self.fail("The remote command did not report its status.")
            self.assertEqual(answer, offload.ReportedMacStatus(0, offload.CleanupRemovedWithoutFacts()))
            offload.report_cleanup(answer.cleanup, "", path="unused", free_floor_gib=100)
        self.assertEqual(output.getvalue(), "")

    def test_cleanup_reports_one_removal_line_with_the_size_and_free_space(self) -> None:
        for size, free, text in (
            (offload.MeasuredCleanupSize(32715571), 92, "31.2 GiB; Mac free 92 GiB"),
            (offload.CleanupSizeNotMeasured(), 92.5, "size not measured; Mac free 92.5 GiB"),
        ):
            with self.subTest(size=size):
                output = io.StringIO()
                with redirect_stdout(output):
                    offload.report_cleanup(offload.CleanupDone(size, free), "",
                                           path="~/.local/state/mac-test/test-mirror/hana/target", free_floor_gib=100)
                self.assertEqual(output.getvalue(),
                                 "Mac build output removed: ~/.local/state/mac-test/test-mirror/hana/target, "
                                 + text + " under the 100 GiB floor\n")


    def test_nextest_summary_scan_bounds_long_lines(self) -> None:
        summary = b"Summary [0.1s] 1 test run: 1 failed, "
        noise = b"x" * (64 * 1024)
        fake_summary = b"Summary [0.2s] 1 test run: 1 passed"
        limits = nextest_log.NextestOutputLimits(tail_bytes=4, summary_bytes=64)
        colored = b"\033[31m" + summary + noise + b"\033[0m"
        cases = (
            ("long Summary", colored + b"\n" + noise + fake_summary, (summary + noise)[:limits.summary_bytes]),
            ("Summary after redraw", noise + b"\r" + colored, (summary + noise)[:limits.summary_bytes]),
            ("Summary overwritten", colored + b"\rtest output", b""),
            ("trailing returns", colored + b"\r\033[0m\r", (summary + noise)[:limits.summary_bytes]),
        )
        for label, contents, expected in cases:
            with self.subTest(label=label):
                output = io.BytesIO()
                log = io.BytesIO(contents + b"\nend\n")
                with mock.patch.object(log, "readline", wraps=log.readline) as readline:
                    nextest_log.replay_log(log, Path("log"), 42, limits, output)
                self.assertTrue(readline.called)
                for call in readline.call_args_list:
                    (size,) = cast(tuple[int], call.args)
                    self.assertTrue(0 < size < len(noise))
                self.assertEqual(output.getvalue(),
                                 b"mac_test: full nextest log: log\nmac_test: tail fragment: end\n"
                                 + (expected + b"\n" if expected else b""))


class MacTargetCleanupShellTests(unittest.TestCase):
    root: Path = Path()
    home_directory: Path = Path()
    mirror: Path = Path()
    temporary_directory: Path = Path()
    bin_directory: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        configuration = self.root / "config"
        configuration.mkdir()
        _ = (configuration / "build_targets.conf").write_text(MAC_TARGET_CONFIG)
        mac_settings = configuration / "mac_test.conf"
        _ = mac_settings.write_text("mirror_root=~/.local/state/mac-test/mirror\n" + nextest_settings())
        _ = (configuration / "lint.conf").write_text(LINT_TRIM_CONFIG)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(configuration)})))
        _ = cast(object, self.enterContext(mock.patch.object(nextest_log, "DEFAULT_SETTINGS", mac_settings)))
        self.home_directory = self.root / "home with spaces"
        self.mirror = self.home_directory / offload.remote_mirror(eligibility.KnownRepository("hana's project"))
        self.mirror.mkdir(parents=True)
        (self.mirror / floor_claims.MIRROR_MARK).touch()
        self.temporary_directory = self.root / "tmp"
        self.temporary_directory.mkdir()
        self.bin_directory = self.root / "bin"
        self.bin_directory.mkdir()
        python = self.home_directory / ".claude/scripts/lib/py"
        python.parent.mkdir(parents=True)
        interpreter, _, body = fixture_shim_source().partition("\n")
        launcher = r'''
case ${2##*/} in
    nextest_log.py)
        [ "$1" = -B ] && [ "$3" = run ] || exit 99
        shift 5
        "$@"
        printf 'mac_exit=%s\n' "$?"
        exit 0
        ;;
    -c)
        [ "$1" = -B ] || exit 99
        case "$CLEANUP_TEST_TRIM" in
            run|refuse) ;;
            report)
                printf '%s\n' 'discard this line' 'mac_cleanup=kept'
                printf 'refusal\t%s\033%s%0300d: output lock is busy\r\n\r\n \t\r\n' 'literal $() %s' 'x' 0 >&2
                exit 7
                ;;
            *) exit "$CLEANUP_TEST_TRIM" ;;
        esac
        shift 2
        set -- -B -c ''' + shlex.quote(TRIM_FIXTURE) + r''' "$@"
        ;;
    *) exit 99 ;;
esac
'''
        _ = python.write_text(interpreter + "\n" + launcher + body)
        python.chmod(0o755)
        disk_space = self.bin_directory / "df"
        write_shell(disk_space, r'''
json_args "$@" > "$CLEANUP_TEST_DIR/df-arguments.json"
printf '%s' "$CLEANUP_TEST_DF_OUTPUT"
exit "$CLEANUP_TEST_DF_STATUS"
''')
        lint = self.home_directory / ".claude/scripts/lint/lint"
        lint.parent.mkdir(parents=True)
        write_shell(lint, r'''
json_args "$@" > "$CLEANUP_TEST_DIR/lint-arguments.json"
printf '%s' "${LINT_SKIP_SWEEP:-}" > "$CLEANUP_TEST_DIR/sweep-switch"
target=target
while (( $# )); do
    case $1 in
        --target-dir) target=$2; shift ;;
        --target-dir=*) target=${1#*=} ;;
    esac
    shift
done
mkdir -p -- "$target" || exit 99
printf '{}\n' > "$target/.rustc_info.json"
printf 'built\n' > "$target/nextest-output"
printf 'nextest finished\n'
exit "$CLEANUP_TEST_STATUS"
''')

    def run_remote(
        self, shell: list[str], status: int, words: tuple[str, ...] = (), *,
        disk_output: str = "Filesystem GiB Used Available Capacity Mounted\nfixture 500 401 99 80% /\n",
        disk_status: int = 0,
        free_floor_gib: float = 100,
        abandoned_session_minutes: int = 7,
        budget_gib: str = "24",
        budget_values: tuple[tuple[str, str], ...] = (),
        trim: int | Literal["run", "refuse", "report"] = 0,
        inherit_lease: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        request = offload.OffloadRequest(self.root, "hana", "cleanup-call", offload.NO_RESULT_FILE, words, owner="verify", holder_session="fixture-session")
        settings = self.root / "command-mac.conf"
        overrides = dict(budget_values)
        _ = settings.write_text(nextest_settings(overrides.get(nextest_log.TAIL_BYTES, "65536"),
                                                overrides.get(nextest_log.SUMMARY_BYTES, "1024")))
        config = eligibility.OffloadConfig(
            enabled=True, connection=eligibility.SshConnectionSettings("unused-host", 6, 10, UNREACHABLE_WAIT_FIXTURE_SECONDS), probe_timeout_s=1,
            unreachable_backoff_s=300, free_floor_gib=free_floor_gib,
            default_target_budget_gib=budget_gib,
            values={"abandoned_session_minutes": str(abandoned_session_minutes), **overrides},
            settings=conf.read(settings),
        )

        text = (
            '[ "$(command -v df)" = ' + shlex.quote(str(self.bin_directory / "df")) + ' ] || exit 99\n'
            + offload.remote_test_command(config, request, eligibility.KnownRepository("hana's project"))
        )
        with build_folders.mac_mirror_hold(self.mirror, build_folders.MacMirrorHold.SHARED,
                                           root=self.mirror.parent) as lease:
            assert isinstance(lease, int), lease
            return subprocess.run(
                [*shell, "-c", text], cwd=self.home_directory, pass_fds=(lease,) if inherit_lease else (),
                env={**shell_environment(self.root),
                     "CONF_DIR": os.environ["CONF_DIR"],
                     "CLEANUP_TEST_DIR": str(self.root), "CLEANUP_TEST_STATUS": str(status),
                     "CLEANUP_TEST_DF_OUTPUT": disk_output, "CLEANUP_TEST_DF_STATUS": str(disk_status),
                     "CLEANUP_TEST_TRIM": str(trim),
                     floor_claims.MIRROR_LEASE_FDS: str(lease) if inherit_lease else ""},
                capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
            )

    def cleanup_lines(self, answer: subprocess.CompletedProcess[str], *, trim_status: int = 0) -> list[str]:
        lines = [line for line in answer.stdout.splitlines()
                 if not line.startswith("mac_test: full nextest log: ")]
        if lines[-1] == "mac_cleanup=skipped":
            return lines
        self.assertRegex(lines[2], rf"^mac_trim=status={trim_status} elapsed_seconds=[0-9]+ "
                         + r"over_budget=(yes|no|unknown) size_kib=([0-9]+|unknown) budget_gib=\S+ detail=[ -~]{0,240}$")
        return lines[:2] + lines[3:]

    def assert_guarded_paths(self, *paths: Path) -> None:
        self.assertEqual(json.loads((self.root / "guarded-paths.json").read_text()), list(map(str, paths)))
        self.assertFalse((self.home_directory / removal_guard.LIST_DIRECTORY).exists())

    def incremental_output(self, name: str, used: float) -> Path:
        profile = self.mirror / "target/debug"
        (profile / ".fingerprint").mkdir(parents=True, exist_ok=True)
        output = profile / "incremental" / name / "output.o"
        output.parent.mkdir(parents=True, exist_ok=True)
        _ = output.write_bytes(os.urandom(64 * 1024))
        os.utime(output, (used, used))
        return output

    def test_unusable_log_directory_refuses_setup_before_lint(self) -> None:
        _ = install_nextest_log_writer(self.home_directory)
        path = self.home_directory / nextest_log.DIRECTORY
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("a regular file blocks log setup")
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_remote(shell, 42, ("--target-dir=other-target",))
                self.assertEqual(answer.stdout.splitlines(), ["mac_nextest_setup=failed", "mac_cleanup=skipped"])
                self.assertIn("cannot start nextest with a log", answer.stderr)
                self.assertFalse((self.root / "lint-arguments.json").exists())

    def test_python_shim_start_failure_declines_but_child_status_is_preserved(self) -> None:
        python = install_nextest_log_writer(self.home_directory)
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (75, 127):
                with self.subTest(shell=shell, source="nextest", status=status):
                    answer = self.run_remote(shell, status, ("--target-dir=other-target",))
                    self.assertEqual(answer.stdout.splitlines()[-2:], [f"mac_exit={status}", "mac_cleanup=skipped"])
        (self.root / "lint-arguments.json").unlink()
        write_shell(python, "exit 127\n")
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell, source="shim"):
                answer = self.run_remote(shell, 42, ("--target-dir=other-target",))
                self.assertEqual(answer.stdout.splitlines(), ["mac_nextest_setup=failed", "mac_cleanup=skipped"])
                self.assertFalse((self.root / "lint-arguments.json").exists())

    def test_nextest_replays_success_and_retains_bounded_failure_output(self) -> None:
        _ = install_nextest_log_writer(self.home_directory)
        summary = "     Summary [  0.1s] 1 test run: 1 failed, " + "detail " * 30
        fragment = "mac_exit=99\nlast output"
        output = "     Summary [  0.0s] 1 test run: 1 passed\n" + "\033[31m" + summary + "\033[0m\n"
        output += "earlier output\n" * 8 + "earlier " + fragment
        _ = (self.root / "nextest-out").write_text(output)
        lint = self.home_directory / ".claude/scripts/lint/lint"
        write_shell(lint, 'cat "$CLEANUP_TEST_DIR/nextest-out" >&2\nexit "$CLEANUP_TEST_STATUS"\n')
        limits = ((nextest_log.TAIL_BYTES, str(len(fragment))), (nextest_log.SUMMARY_BYTES, "52"))
        logs = self.home_directory / nextest_log.DIRECTORY
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                failed = self.run_remote(shell, 42, ("--target-dir=other-target",), budget_values=limits)
                self.assertTrue(failed.stdout.startswith("mac_test: full nextest log: "), failed.stdout + failed.stderr)
                self.assertEqual((failed.returncode, failed.stderr), (0, ""))
                log, = logs.iterdir()
                self.assertRegex(log.name, nextest_log.LOG_NAME)
                self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
                self.assertEqual(log.read_text(), output)
                self.assertEqual(failed.stdout, (
                    f"mac_test: full nextest log: {log}\n"
                    + f"mac_test: tail fragment: {fragment}\n{summary[:52]}\n"
                    + "mac_exit=42\nmac_cleanup=skipped\n"
                ))

                passed = self.run_remote(shell, 0, ("--target-dir=other-target",), budget_values=limits)
                self.assertEqual((passed.returncode, passed.stderr), (0, ""))
                self.assertEqual(passed.stdout, output + "\nmac_exit=0\nmac_cleanup=skipped\n")
                self.assertEqual(list(logs.iterdir()), [log])
                log.unlink()

    def test_remote_text_removes_only_old_abandoned_sessions_before_building(self) -> None:
        minutes = 3
        cases: tuple[tuple[str, int, bool, tuple[str, ...]], ...] = (
            ("debug/incremental/crate/s-old-working", minutes + 2, False, ()),
            ("aarch64-apple-darwin/debug/incremental/crate/s-old-working", minutes + 2, False, ()),
            ("debug/incremental/crate/s-new-working", 1, True, ()),
            ("aarch64-apple-darwin/debug/incremental/crate/s-new-working", 1, True, ()),
            ("debug/incremental/crate/s-finished", minutes + 2, True, ()),
            ("aarch64-apple-darwin/debug/incremental/crate/s-finished", minutes + 2, True, ()),
            *((f"debug/{name}/incremental/crate/s-old-working", minutes + 2, True, ())
              for name in ("deps", "build", ".fingerprint", "examples")),
            ("deep/triple/debug/incremental/crate/s-old-working", minutes + 2, True, ()),
            ("debug/incremental/crate/s-old-working", minutes + 2, True, ("--target-dir", "other-target")),
            ("debug/incremental/crate/s-old-working", minutes + 2, True, ("--target-dir=other-target",)),
            ("", 0, False, ()),
        )
        target = self.mirror / "target"
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for relative, age, keep, words in cases:
                for status in (0, 42):
                    with self.subTest(shell=shell, session=relative, words=words, status=status):
                        shutil.rmtree(target, ignore_errors=True)
                        session = target / relative
                        lock = session.with_name(session.name.removesuffix("-working") + ".lock")
                        if relative:
                            session.mkdir(parents=True)
                            _ = (session / "compiler-output").write_text("session data")
                            _ = lock.write_text("session lock")
                            modified = time.time() - age * 60
                            os.utime(session, (modified, modified))
                            os.utime(lock, (modified, modified))
                        answer = self.run_remote(shell, status, words, free_floor_gib=50,
                                                 abandoned_session_minutes=minutes)
                        self.assertEqual((answer.returncode, answer.stderr), (0, ""), answer.stdout)
                        cleanup = "skipped" if words else "kept"
                        self.assertEqual(self.cleanup_lines(answer),
                                         ["nextest finished", f"mac_exit={status}", f"mac_cleanup={cleanup}"])
                        build_target = self.mirror / "other-target" if words else target
                        self.assertEqual((build_target / "nextest-output").read_text(), "built\n")
                        if relative:
                            self.assertEqual(session.exists(), keep)
                            self.assertEqual(lock.exists(), keep)
                            if keep:
                                self.assertEqual((session / "compiler-output").read_text(), "session data")
                                self.assertEqual(lock.read_text(), "session lock")

    def test_remote_text_below_space_floor_trims_only_old_output_after_pass_and_failure(self) -> None:
        target = self.mirror / "target"
        sibling = self.mirror / "keep-source"
        temporary_file = self.temporary_directory / "keep-temporary"
        other_target = self.mirror.parent / "other-repository" / "target" / "keep-build"
        other_target.parent.mkdir(parents=True)
        for path in (sibling, temporary_file, other_target):
            _ = path.write_text("keep\n")
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                with self.subTest(shell=shell, status=status):
                    now = time.time()
                    old = self.incremental_output("old-session", now - 7200)
                    new = self.incremental_output("new-session", now)
                    _ = (target / "previous-build").write_text("old\n")
                    identity = target.stat()
                    answer = self.run_remote(shell, status, budget_gib=str(96 / 1024 ** 2), trim="run")
                    self.assertEqual((answer.returncode, answer.stderr), (0, ""), answer.stdout)
                    self.assertEqual(self.cleanup_lines(answer),
                                     ["nextest finished", f"mac_exit={status}", "mac_cleanup=kept"])
                    self.assertTrue(target.is_dir(), target)
                    self.assertTrue(os.path.samestat(identity, target.stat()))
                    self.assertFalse(old.parent.exists())
                    self.assertEqual(len(new.read_bytes()), 64 * 1024)
                    self.assertEqual((target / "previous-build").read_text(), "old\n")
                    self.assertEqual((target / "nextest-output").read_text(), "built\n")
                    self.assert_guarded_paths(old.parent)
                    self.assertRegex(answer.stdout, r"mac_trim=status=0 elapsed_seconds=[0-9]+ over_budget=no")
                    self.assertEqual((self.root / "sweep-switch").read_text(), "1")
                    self.assertFalse((self.root / "df-arguments.json").exists())
                    for path in (sibling, temporary_file, other_target):
                        self.assertEqual(path.read_text(), "keep\n")

        with self.subTest(environment="private"):
            with mock.patch.dict(os.environ, {
                "GIT_DIR": str(self.root / "caller-git"),
                "GIT_WORK_TREE": str(self.root / "caller-worktree"),
                "LINT_SWEEP_STATE_DIR": str(self.root / "caller-state"),
                "LINT_SWEEP_DOC_INDEX_MIB": "999",
            }):
                answer = self.run_remote(["/bin/sh"], 0, trim="run")
            self.assertEqual((answer.returncode, answer.stderr), (0, ""), answer.stdout)
            self.assertEqual(self.cleanup_lines(answer)[:2], ["nextest finished", "mac_exit=0"])
            environment = cast(dict[str, str], json.loads((self.root / "trim-environment.json").read_text()))
            self.assertNotIn("GIT_DIR", environment)
            self.assertNotIn("GIT_WORK_TREE", environment)
            self.assertEqual(environment["LINT_SWEEP_STATE_DIR"], str(self.root / "sweep-state"))
            self.assertEqual(environment["LINT_SWEEP_DOC_INDEX_MIB"], DOC_INDEX_MIB)

    def test_failed_size_measurement_keeps_the_target_under_sh_and_zsh(self) -> None:
        write_shell(self.bin_directory / "du", r'''
json_args "$@" > "$CLEANUP_TEST_DIR/du-arguments.json"
printf '12\t%s\n' "$2"
exit 1
''')
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_remote(shell, 0, trim="run")
                self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                self.assertEqual(self.cleanup_lines(answer),
                                 ["nextest finished", "mac_exit=0", "mac_cleanup=kept"])
                self.assertEqual(answer.stderr, "")
                self.assertRegex(answer.stdout, r"mac_trim=status=0 elapsed_seconds=[0-9]+ over_budget=unknown")
                self.assertEqual((self.mirror / "target" / "nextest-output").read_text(), "built\n")
                self.assert_guarded_paths()
                self.assertEqual(json.loads((self.root / "du-arguments.json").read_text()),
                                 ["-sk", str(self.mirror / "target")])

    def test_remote_trim_uses_repository_budget_and_keeps_the_target(self) -> None:
        target = self.mirror / "target"
        target.mkdir()
        existing = target / "previous-build"
        _ = existing.write_text("keep\n")
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                for free_space, floor in ((100, 100), (101, 100), (50, 50)):
                    with self.subTest(shell=shell, status=status, free_space=free_space, floor=floor):
                        answer = self.run_remote(
                            shell, status, free_floor_gib=floor,
                            disk_output=f"Filesystem GiB Used Available Capacity Mounted\nfixture 500 400 {free_space} 80% /\n",
                        )
                        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                        self.assertEqual(self.cleanup_lines(answer), ["nextest finished", f"mac_exit={status}", "mac_cleanup=kept"])
                        self.assertEqual(existing.read_text(), "keep\n")
                        self.assertEqual((target / "nextest-output").read_text(), "built\n")
                        self.assertFalse((self.root / "df-arguments.json").exists())

        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                for default_kib, repository_kib, trimmed in ((96, 0, True), (256, 96, True), (96, 256, False)):
                    with self.subTest(shell=shell, status=status, default_kib=default_kib,
                                      repository_kib=repository_kib):
                        shutil.rmtree(target)
                        now = time.time()
                        old = self.incremental_output("old-session", now - 7200)
                        new = self.incremental_output("new-session", now - 600)
                        identity = target.stat()
                        overrides = (("mac_budget_gib.hana's project", str(repository_kib / 1024 ** 2)),) if repository_kib else ()
                        answer = self.run_remote(
                            shell, status, free_floor_gib=100,
                            disk_output="Filesystem GiB Used Available Capacity Mounted\nfixture 500 300 200 60% /\n",
                            budget_gib=str(default_kib / 1024 ** 2), budget_values=overrides, trim="run",
                        )
                        self.assertEqual((answer.returncode, answer.stderr), (0, ""), answer.stdout)
                        self.assertEqual(self.cleanup_lines(answer),
                                         ["nextest finished", f"mac_exit={status}", "mac_cleanup=kept"])
                        self.assertTrue(target.is_dir(), target)
                        self.assertTrue(os.path.samestat(identity, target.stat()))
                        self.assertEqual(old.exists(), not trimmed)
                        self.assert_guarded_paths(*((old.parent,) if trimmed else ()))
                        if not trimmed:
                            self.assertEqual(old.stat().st_size, 64 * 1024)
                        self.assertEqual(len(new.read_bytes()), 64 * 1024)
                        self.assertRegex(answer.stdout, r"mac_trim=status=0 elapsed_seconds=[0-9]+ over_budget=no")

        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for trim_fails in (False, True):
                with self.subTest(shell=shell, trim_fails=trim_fails):
                    shutil.rmtree(target, ignore_errors=True)
                    target.mkdir()
                    retained = target / "unmatched-output"
                    _ = retained.write_bytes(os.urandom(128 * 1024))
                    answer = self.run_remote(
                        shell, 42, budget_gib=str(96 / 1024 ** 2),
                        trim=9 if trim_fails else "run",
                        disk_output=("Filesystem GiB Used Available Capacity Mounted\n"
                                     + f"fixture 500 300 {99 if trim_fails else 200} 60% /\n"),
                    )
                    self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                    self.assertEqual(retained.stat().st_size, 128 * 1024)
                    trim_status = 9 if trim_fails else 1
                    self.assertEqual(self.cleanup_lines(answer, trim_status=trim_status),
                                     ["nextest finished", "mac_exit=42", f"mac_cleanup={trim_status}"])
                    size = subprocess.check_output(["du", "-sk", str(target)], text=True).split()[0]
                    budget = re.escape(str(96 / 1024 ** 2))
                    self.assertRegex(answer.stdout,
                                     rf"mac_trim=status={trim_status} elapsed_seconds=[0-9]+ "
                                     + rf"over_budget=yes size_kib={size} budget_gib={budget}")
                    marker = next(line for line in answer.stdout.splitlines() if line.startswith("mac_trim="))
                    detail = marker.split(" detail=", 1)[1]
                    if trim_fails:
                        self.assertEqual(detail, "")
                    else:
                        self.assertTrue(detail.startswith("lint sweep: over budget: "), detail)
                        self.assert_guarded_paths()

        with self.subTest(user_zone="caller has no zone"):
            shutil.rmtree(target, ignore_errors=True)
            modified = datetime(2020, 2, 3, 4, 5, tzinfo=UTC).timestamp()
            old = self.incremental_output("old-session", modified)
            with mock.patch.dict(os.environ):
                _ = os.environ.pop("USER_ZONE", None)
                answer = self.run_remote(["/bin/sh"], 0, free_floor_gib=50, budget_gib=str(32 / 1024 ** 2), trim="run")
            self.assertEqual((answer.returncode, answer.stderr), (0, ""), answer.stdout)
            self.assertEqual(self.cleanup_lines(answer), ["nextest finished", "mac_exit=0", "mac_cleanup=kept"])
            self.assertFalse(old.parent.exists())
            self.assertEqual((target / "nextest-output").read_text(), "built\n")
            self.assert_guarded_paths(old.parent)
            self.assertNotIn("user_zone", (self.root / "trim-config-reads").read_text().splitlines())

    def test_remote_text_with_unreadable_space_keeps_build_output(self) -> None:
        target = self.mirror / "target"
        target.mkdir()
        existing = target / "previous-build"
        _ = existing.write_text("keep\n")
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                for disk_output, disk_status in (("", 0), ("header\nfixture 500 400 unavailable\n", 0), ("", 1)):
                    with self.subTest(shell=shell, status=status, disk_output=disk_output, disk_status=disk_status):
                        answer = self.run_remote(shell, status, disk_output=disk_output, disk_status=disk_status)
                        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                        self.assertEqual(self.cleanup_lines(answer), ["nextest finished", f"mac_exit={status}", "mac_cleanup=kept"])
                        self.assertEqual(existing.read_text(), "keep\n")
                        self.assertEqual((target / "nextest-output").read_text(), "built\n")
                        self.assertFalse((self.root / "df-arguments.json").exists())

    def test_remote_text_with_another_target_dir_keeps_every_build_directory(self) -> None:
        target = self.mirror / "target"
        target.mkdir()
        existing = target / "previous-build"
        _ = existing.write_text("keep\n")
        other_target = self.temporary_directory / "other build"
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                for words in (("--target-dir", str(other_target)), (f"--target-dir={other_target}",)):
                    with self.subTest(shell=shell, status=status, words=words):
                        answer = self.run_remote(shell, status, words)
                        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                        self.assertEqual(self.cleanup_lines(answer), ["nextest finished", f"mac_exit={status}", "mac_cleanup=skipped"])
                        self.assertEqual(existing.read_text(), "keep\n")
                        self.assertEqual((other_target / "nextest-output").read_text(), "built\n")
                        self.assertEqual(cast(list[str], json.loads((self.root / "lint-arguments.json").read_text())), ["nextest", *words])

    def test_remote_text_reports_guard_refusal_after_the_test_status(self) -> None:
        remove = self.bin_directory / "rm"
        write_shell(remove, r'''
json_args "$@" > "$CLEANUP_TEST_DIR/remove-arguments.json"
exit 7
''')
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                with self.subTest(shell=shell, status=status):
                    old = self.incremental_output("old-session-" + "x" * 230, time.time() - 7200)
                    self.assertGreater(len(str(old.parent)), 240)
                    answer = self.run_remote(shell, status, budget_gib=str(32 / 1024 ** 2), trim="refuse")
                    self.assertEqual((answer.returncode, answer.stderr), (0, ""), answer.stdout)
                    self.assertEqual(self.cleanup_lines(answer, trim_status=1),
                                     ["nextest finished", f"mac_exit={status}", "mac_cleanup=1"])
                    marker = next(line for line in answer.stdout.splitlines() if line.startswith("mac_trim="))
                    detail = marker.split(" detail=", 1)[1]
                    self.assertTrue(detail.startswith(removal_guard.RefusalReason.MOUNT.value), detail)
                    self.assert_guarded_paths(old.parent)
                    self.assertEqual(old.stat().st_size, 64 * 1024)
                    self.assertEqual((self.mirror / "target" / "nextest-output").read_text(), "built\n")
                    self.assertFalse((self.root / "remove-arguments.json").exists())

    def test_remote_trim_reports_only_the_last_line_with_a_bounded_detail(self) -> None:
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_remote(shell, 42, trim="report")
                self.assertEqual((answer.returncode, answer.stderr), (0, ""), answer.stdout)
                self.assertEqual(self.cleanup_lines(answer, trim_status=7),
                                 ["nextest finished", "mac_exit=42", "mac_cleanup=7"])
                marker = next(line for line in answer.stdout.splitlines() if line.startswith("mac_trim="))
                detail = marker.split(" detail=", 1)[1]
                prefix = "refusal literal $() %s x"
                suffix = ": output lock is busy"
                self.assertEqual(detail, prefix + "0" * (119 - len(prefix))
                                 + "..." + "0" * (118 - len(suffix)) + suffix)

    def test_remote_trim_names_the_invalid_recent_minutes_before_a_long_config_path(self) -> None:
        config = self.root / ("c" * 125) / ("d" * 125)
        config.parent.mkdir()
        config.symlink_to(self.root / "config", target_is_directory=True)
        settings = config / "lint.conf"
        self.assertGreater(len(str(settings)), 240)
        _ = settings.write_text(LINT_TRIM_CONFIG.replace("recent_minutes=60", "recent_minutes=invalid"))
        old = self.incremental_output("old-session", time.time() - 7200)
        with mock.patch.dict(os.environ, {"CONF_DIR": str(config)}):
            for shell in (["/bin/sh"], ["zsh", "-f"]):
                with self.subTest(shell=shell):
                    answer = self.run_remote(shell, 42, budget_gib=str(32 / 1024 ** 2), trim="run")
                    self.assertEqual((answer.returncode, answer.stderr), (0, ""), answer.stdout)
                    self.assertEqual(self.cleanup_lines(answer, trim_status=1),
                                     ["nextest finished", "mac_exit=42", "mac_cleanup=1"])
                    marker = next(line for line in answer.stdout.splitlines() if line.startswith("mac_trim="))
                    detail = marker.split(" detail=", 1)[1]
                    self.assertTrue(detail.startswith("cannot read Mac trim settings: recent_minutes='invalid': "
                                                      + "expected a finite number"), detail)
                    self.assertTrue(detail.endswith("/lint.conf)"), detail)
                    self.assertEqual(len(detail), 240)
                    self.assert_guarded_paths()
                    self.assertTrue(old.is_file())

    def test_remote_trim_keeps_the_refusal_for_a_path_with_control_characters(self) -> None:
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                old = self.incremental_output("newline\nfixture\x1bname", time.time() - 7200)
                answer = self.run_remote(shell, 42, budget_gib=str(32 / 1024 ** 2), trim="refuse")
                self.assertEqual((answer.returncode, answer.stderr), (0, ""), answer.stdout)
                self.assertEqual(self.cleanup_lines(answer, trim_status=1),
                                 ["nextest finished", "mac_exit=42", "mac_cleanup=1"])
                marker = next(line for line in answer.stdout.splitlines() if line.startswith("mac_trim="))
                detail = marker.split(" detail=", 1)[1]
                self.assertTrue(detail.startswith(removal_guard.RefusalReason.MOUNT.value), detail)
                self.assert_guarded_paths(old.parent)
                self.assertTrue(old.is_file())

    def test_remote_trim_names_the_missing_lease_under_a_long_mirror_root(self) -> None:
        relative_root = "/".join(("m" * 125, "n" * 125))
        mac_settings = self.root / "config/mac_test.conf"
        _ = mac_settings.write_text(f"mirror_root=~/{relative_root}\n" + nextest_settings())
        self.mirror = self.home_directory / offload.remote_mirror(eligibility.KnownRepository("hana's project"))
        self.mirror.mkdir(parents=True)
        (self.mirror / floor_claims.MIRROR_MARK).touch()

        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_remote(shell, 42, trim="run", inherit_lease=False)
                self.assertEqual((answer.returncode, answer.stderr), (0, ""), answer.stdout)
                self.assertEqual(self.cleanup_lines(answer, trim_status=1),
                                 ["nextest finished", "mac_exit=42", "mac_cleanup=1"])
                marker = next(line for line in answer.stdout.splitlines() if line.startswith("mac_trim="))
                detail = marker.split(" detail=", 1)[1]
                self.assertTrue(detail.startswith("no inherited Mac floor lease: "), detail)
                self.assertEqual(len(detail), 240)
                self.assertEqual((self.mirror / "target" / "nextest-output").read_text(), "built\n")

    def test_remote_trim_names_an_over_budget_size_after_a_silent_success(self) -> None:
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_remote(shell, 42, budget_gib="0", trim=0)
                self.assertEqual((answer.returncode, answer.stderr), (0, ""), answer.stdout)
                self.assertEqual(self.cleanup_lines(answer, trim_status=1),
                                 ["nextest finished", "mac_exit=42", "mac_cleanup=1"])
                marker = next(line for line in answer.stdout.splitlines() if line.startswith("mac_trim="))
                self.assertIn(" over_budget=yes ", marker)
                self.assertTrue(marker.endswith(" detail=output above budget after trim"), marker)


class MacActivityShellTests(unittest.TestCase):
    root: Path = Path()
    bin_directory: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        configuration = self.root / "config"
        configuration.mkdir()
        _ = (configuration / "build_targets.conf").write_text(MAC_TARGET_CONFIG)
        _ = (configuration / "mac_test.conf").write_text("mirror_root=~/.local/state/mac-test/test-mirror\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(configuration)})))
        self.bin_directory = self.root / "bin"
        self.bin_directory.mkdir()
        for name in ("pgrep", "lsof", "ps", "id"):
            path = self.bin_directory / name
            write_shell(path, ACTIVITY_SELECTOR_STUB)

    def run_activity(self, shell: list[str], responses: dict[str, tuple[int, str]]) -> subprocess.CompletedProcess[str]:
        for name, (status, answer) in responses.items():
            _ = (self.root / f"{name}-response.sh").write_text(
                f"status={status}\nanswer={shlex.quote(answer)}\n")
        (self.root / "commands.jsonl").unlink(missing_ok=True)
        environment = {**shell_environment(self.root), "ACTIVITY_TEST_DIR": str(self.root)}
        checks = "\n".join(
            '[ "$(command -v ' + name + ')" = ' + shlex.quote(str(self.bin_directory / name)) + ' ] || exit 99'
            for name in ("pgrep", "lsof", "ps", "id")
        )
        text = "kill() { printf 'signal requested\\n' >&2; return 99; }\n" + checks + "\n" + mac_test.mac_activity_script()
        return subprocess.run([*shell, "-c", text], env=environment, capture_output=True, text=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)

    def test_activity_text_has_zero_for_only_shells_in_clones_under_sh_and_zsh(self) -> None:
        responses = {
            "id": (0, "501\n"), "pgrep": (1, ""),
            "lsof": (0, "p101\nn/Users/test/rust/hana\np102\nn/Users/test/rust/other\n"),
            "ps": (0, "101 501 /bin/zsh\n102 501 /bin/sh\n103 999 /opt/cargo\n"),
        }
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_activity(shell, responses)
                self.assertEqual((answer.returncode, answer.stdout, answer.stderr), (0, "active=0\nwork=0\nrc=0\n", ""))

    def test_activity_text_counts_each_matching_user_pid_once_under_sh_and_zsh(self) -> None:
        responses = {
            "id": (0, "501\n"), "pgrep": (0, "101\n102\n"),
            "lsof": (0, "p102\nn/Users/test/.local/state/mac-test/test-mirror/hana\n"
                     + "p103\nn/Users/test/.local/state/mac-test/test-mirror\np104\nn/Users/test/rust/hana\n"),
            "ps": (0, "101 501 /opt/rust/bin/cargo\n105 501 /opt/rust/bin/rustc\n106 501 cargo-nextest\n"
                   + "107 501 /Users/test/rust/hana/target/debug/app\n108 999 /opt/cargo\n"
                   + "109 501 /bin/zsh\n110 501 /opt/cargo-helper\n112 501 /Users/test/my project/target/debug/app\n"),
        }
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_activity(shell, responses)
                self.assertEqual((answer.returncode, answer.stdout, answer.stderr), (0, "active=7\nwork=5\nrc=0\n", ""))
                calls = [cast(SelectorEvent, json.loads(line)) for line in (self.root / "commands.jsonl").read_text().splitlines()]
                pgrep = [call for call in calls if call["command"] == "pgrep"]
                lsof = [call for call in calls if call["command"] == "lsof"]
                ps = [call for call in calls if call["command"] == "ps"]
                self.assertEqual(len(pgrep), 1)
                self.assertIn("-u", pgrep[0]["args"])
                self.assertIn("501", pgrep[0]["args"])
                self.assertIn("[.]local/state/mac-test/test-mirror/", pgrep[0]["args"])
                self.assertEqual(lsof[0]["args"], ["-a", "-u", "501", "-d", "cwd", "-Fpn"])
                self.assertEqual(len(ps), 1)

    def test_activity_selector_failure_prints_no_success_under_sh_and_zsh(self) -> None:
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for name, status in (("pgrep", 2), ("lsof", 1), ("ps", 1)):
                with self.subTest(shell=shell, selector=name):
                    responses = {"id": (0, "501\n"), "pgrep": (1, ""), "lsof": (0, ""), "ps": (0, "")}
                    responses[name] = (status, "")
                    answer = self.run_activity(shell, responses)
                    self.assertNotEqual(answer.returncode, 0)
                    self.assertNotIn("rc=0", answer.stdout)
                    self.assertNotIn("active=0", answer.stdout)
                    self.assertNotIn("signal requested", answer.stderr)

    def test_activity_and_leftover_texts_use_the_shared_mirror_selector(self) -> None:
        marker = "select_leftovers() { return 1; }\n"
        root = MacMirrorRoot("saved/mirror")
        with mock.patch.object(offload, "mirror_selector_script", return_value=marker) as selector:
            self.assertTrue(offload.leftover_check_script(HANA).startswith(marker))
            self.assertTrue(mac_test.mac_activity_script().startswith(marker))
            observation = offload.leftover_check_script(HANA, observe=True, mirror_root=root)
            self.assertTrue(observation.startswith(marker))
        self.assertEqual(selector.call_args_list, [mock.call(HANA, "mac", 1, mirror_root=None),
            mock.call(offload.ALL_MIRRORS), mock.call(HANA, "mac", 1, mirror_root=root)])

    def run_selector(
        self, shell: list[str], scope: offload.MirrorScope, responses: dict[str, tuple[int, str]],
    ) -> subprocess.CompletedProcess[str]:
        for name, (status, answer) in responses.items():
            _ = (self.root / f"{name}-response.sh").write_text(
                f"status={status}\nanswer={shlex.quote(answer)}\n")
        (self.root / "commands.jsonl").unlink(missing_ok=True)
        environment = {**shell_environment(self.root), "ACTIVITY_TEST_DIR": str(self.root)}
        text = offload.mirror_selector_script(scope) + "select_leftovers\n"
        return subprocess.run([*shell, "-c", text], env=environment, capture_output=True, text=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)

    def test_repository_selector_stops_at_its_own_mirror_under_sh_and_zsh(self) -> None:
        responses = {
            "id": (0, "501\n"), "pgrep": (1, ""),
            "lsof": (0, "p201\nn/Users/test/.local/state/mac-test/test-mirror/hana\n"
                     + "p202\nn/Users/test/.local/state/mac-test/test-mirror/hana/crates/hana\n"
                     + "p203\nn/Users/test/.local/state/mac-test/test-mirror/hana_other\n"
                     + "p204\nn/Users/test/.local/state/mac-test/test-mirror/cargo-liner\n"
                     + "p205\nn/Users/test/.local/state/mac-test/test-mirror\n"
                     + "p206\nn/Users/test/rust/hana\n"),
        }
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_selector(shell, HANA, responses)
                self.assertEqual((answer.returncode, answer.stdout, answer.stderr), (0, "201\n202\n", ""))
                calls = [cast(SelectorEvent, json.loads(line)) for line in (self.root / "commands.jsonl").read_text().splitlines()]
                pgrep = [call for call in calls if call["command"] == "pgrep"]
                self.assertEqual(len(pgrep), 1)
                self.assertEqual(pgrep[0]["args"], ["-u", "501", "-f", HANA_COMMAND_PATTERN])

    def test_repository_command_pattern_matches_only_its_own_mirror(self) -> None:
        responses = {"id": (0, "501\n"), "pgrep": (1, ""), "lsof": (0, "")}
        answer = self.run_selector(["/bin/sh"], HANA, responses)
        self.assertEqual(answer.returncode, 0, answer.stderr)
        calls = [cast(SelectorEvent, json.loads(line)) for line in (self.root / "commands.jsonl").read_text().splitlines()]
        pattern = next(call["args"][-1] for call in calls if call["command"] == "pgrep")
        matching = [
            "sh -c cd .local/state/mac-test/test-mirror/hana && cargo nextest run",
            "cargo build --manifest-path /Users/test/.local/state/mac-test/test-mirror/hana/Cargo.toml",
            "sh -c cd '/Users/test/.local/state/mac-test/test-mirror/hana' && cargo test",
            "sh -c cd \"/Users/test/.local/state/mac-test/test-mirror/hana\"",
            "ls /Users/test/.local/state/mac-test/test-mirror/hana",
        ]
        other = [
            "sh -c cd .local/state/mac-test/test-mirror/hana_other && cargo nextest run",
            "sh -c cd .local/state/mac-test/test-mirror/cargo-liner && cargo nextest run",
            "sh -c cd .local/state/mac-test/test-mirror/hanabi && cargo test",
            "sh -c cd /Users/test/rust/hana && cargo test",
        ]
        selected = subprocess.run(["grep", "-E", "-e", pattern], input="\n".join(matching + other) + "\n",
                                  env=shell_environment(self.root),
                                  capture_output=True, text=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(selected.stdout.splitlines(), matching)




if __name__ == "__main__":
    _ = unittest.main()

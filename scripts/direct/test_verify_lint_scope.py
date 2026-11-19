"""Check workspace and package lint commands through the real shell pipeline."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
from typing import cast, override
import unittest
from unittest.mock import patch

from scripts.build_queue import admission_protocol as wire
from scripts.build_queue.admission_test_support import caller_admission
from scripts.copies.copies_test_support import copies_environment
from scripts.lib.git_test_support import commit_repository
from scripts.lib.process_group import send_process_group
from scripts.lib.socket_test_support import ShortUnixSocket
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production.staff_test_support import fixture_member, isolated_staff_file

from scripts.direct import verify_lint_scope_test_support as support
from scripts.direct.verify_lint_scope_test_support import CARGO_STUB as CARGO_STUB
from scripts.direct.wait_for_test_state import wait_for_pid, wait_until


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    def test_cache_reads_each_tool_version_once_per_run_and_rejects_a_changed_version(self) -> None:
        _ = self.enable_cache()
        events = self.root / "version-events"
        self.environment["TEST_VERSION_EVENTS"] = str(events)
        for version, recorded in (("before", False), ("before", True), ("after", False)):
            with self.subTest(version=version, recorded=recorded):
                events.unlink(missing_ok=True)
                self.environment["TEST_VERSION_MEND"] = version
                result = self.verify("lint", "sample")
                self.assertEqual("(recorded)" in result.stdout, recorded)
                self.assertEqual(events.read_text().splitlines(), ["TAPLO", "MEND", "RUSTFMT", "CLIPPY"])
                self.assertEqual(self.clippy_ran(), not recorded)

    def test_uncached_lint_reads_only_the_formatter_availability_version(self) -> None:
        events = self.root / "version-events"
        self.environment["TEST_VERSION_EVENTS"] = str(events)
        _ = self.verify("lint", "sample")
        self.assertEqual(events.read_text().splitlines(), ["RUSTFMT"])

    def test_clean_tree_runs_one_judgment_without_repairs_or_rustdoc(self) -> None:
        result = self.verify("lint")
        self.assertEqual(self.calls("clippy"), [[
            "--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings",
        ]])
        self.assertEqual(self.lint_stages(), [
            ["+nightly", "fmt", "--all"],
            ["clippy", *self.calls("clippy")[0]],
        ])
        self.assertIn("style-lint: clippy start", result.stdout.splitlines())
        self.assertIn("style-lint: clippy passed", result.stdout.splitlines())

    def test_lint_matches_ci_and_preserves_other_stages(self) -> None:
        self.environment["TEST_CLIPPY_STATUSES"] = "[101, 0]"
        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assert_fix_and_judgment_calls()
        self.assertEqual(self.calls("mend"), [["--all-targets", "--workspace", "--fix"]])
        self.assertEqual(self.calls("+nightly"), [
            ["fmt", "--version"], ["fmt", "-p", "sample"],
            ["fmt", "-p", "sample"],
        ])
        self.assertEqual(self.calls("doc"), [])

    def test_clippy_off_names_the_full_scope_without_running_it(self) -> None:
        self.write_config(clippy="off")
        result = self.verify("lint", "sample")
        skip_lines = [line for line in result.stdout.splitlines()
                      if line.startswith("SKIPPED:") and "clippy" in line]
        self.assertEqual(skip_lines, [
            "SKIPPED: cargo clippy --workspace --all-targets --all-features"
            + f" — clippy=off in {self.config}"
        ])
        self.assertEqual(self.calls("clippy"), [])
        self.assertEqual(len(self.calls("mend")), 1)
        self.assertEqual(self.calls("doc"), [])
        self.assert_workspace_taplo_check()



BATCH_CARGO_STUB = CARGO_STUB.replace(
    '    print(os.environ["TEST_METADATA"])',
    '''    metadata = json.loads(os.environ["TEST_METADATA"].replace(os.environ["PACKAGE_ROOT"], str(Path.cwd())))
    for package in metadata["packages"]:
        package["dependencies"] = []
    metadata["target_directory"] = str(Path.cwd() / "target")
    print(json.dumps(metadata))''',
).replace(
    'elif arguments[:1] not in',
    r'''elif arguments[:1] == ["nextest"]:
    if arguments[1:] == ["--version"]:
        raise SystemExit(0)
    taplo_calls = [
        json.loads(line)["args"] for line in
        Path(os.environ["TEST_TAPLO_EVENTS"]).read_text().splitlines()
    ] if Path(os.environ["TEST_TAPLO_EVENTS"]).exists() else []
    expression = arguments[arguments.index("-E") + 1]
    if Path.cwd() != Path(os.environ["PACKAGE_ROOT"]):
        assert expression == "package(sample) & test(=regression)", expression
        assert Path("src/regression.rs").is_file(), Path.cwd()
        assert " { 1 }" in Path("src/lib.rs").read_text()
        if ready := os.environ.get("TEST_BASELINE_READY"):
            from socket import AF_UNIX, SOCK_STREAM, socket
            with socket(AF_UNIX, SOCK_STREAM) as started:
                started.connect(ready)
                started.sendall(os.environ["COPIES_STEP_SOURCE"].encode() + b"\n")
                started.recv(1)
        raise SystemExit(int(os.environ["TEST_BASELINE_STATUS"]))
    assert any("--check" not in args for args in taplo_calls), taplo_calls
    raise SystemExit(int(os.environ.get("TEST_SAMPLE_STATUS", "0")) if expression == "package(sample)" else 0)
elif arguments[:1] not in''',
)


class VerifyBatchTests(support.VerifyLintScopeFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.write_executable(self.root / "bin" / "cargo", BATCH_CARGO_STUB)
        self.environment["CARGO_MAKEFLAGS"] = "--jobserver-auth=fixture"
        self.use_real_git()
        path = self.root / "other" / "Cargo.toml"
        path.parent.mkdir()
        _ = path.write_text('[package]\nname = "other"\nversion = "0.1.0"\n')
        examples = self.root / "examples"
        examples.mkdir()
        for name in ("plain", "featured"):
            _ = (examples / f"{name}.rs").write_text("fn main() {}\n")
        self.run_git("add", "-f", str(path), "examples/plain.rs", "examples/featured.rs")
        self.run_git("-c", "user.name=Batch fixture", "-c", "user.email=batch@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Add test packages")
        metadata = cast(support.CargoWorkspaceMetadata, json.loads(self.environment["TEST_METADATA"]))
        metadata["workspace_members"].append("other-id")
        metadata["packages"].append({
            "name": "other", "id": "other-id", "features": {}, "manifest_path": str(path), "targets": [],
        })
        self.environment["TEST_METADATA"] = json.dumps(metadata)
        self.enable_batch_session()

    def enable_batch_session(self) -> None:
        session = self.root / "session"
        session.mkdir()
        self.environment["UNIT_RUN_DIR"] = str(session)

    def batch_arguments(self, *flags: str, lint_scope: str = "packages") -> tuple[str, ...]:
        return ("batch", "--packages", "sample,other", "--lint", lint_scope, *flags)

    def batch(self, *flags: str, lint_scope: str = "packages") -> subprocess.CompletedProcess[str]:
        return self.invoke_verify(*self.batch_arguments(*flags, lint_scope=lint_scope), cwd=self.root)

    def batch_lines(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        return [line for line in result.stdout.splitlines() if line.startswith("verify.sh batch ")]

    def fail_first(self, *, status: int) -> None:
        options = copies_environment(self.root / "batch-copies", repository=self.root.name)
        folders = Path(self.environment["CONF_DIR"]) / "build_folders.conf"
        _ = folders.write_bytes((Path(options["CONF_DIR"]) / folders.name).read_bytes())
        self.environment.update({name: value for name, value in options.items()
                                 if name not in ("HOME", "CONF_DIR")})
        _ = self.enterContext(isolated_staff_file(self.root / "batch-staff.json", self.environment))
        parent = fixture_member("showrunner", name="builds")
        member = fixture_member("unit", name="batch-fixture", parent=parent.id)
        self.environment["CLAUDE_CODE_SESSION_ID"] = member.current_session
        self.environment["TEST_BASELINE_STATUS"] = str(status)
        phase = Path(self.environment["UNIT_RUN_DIR"]) / "phases" / "fixture"
        phase.mkdir(parents=True)
        self.environment["VERIFY_BATCH_PHASE_DIR"] = str(phase)
        baseline = self.git_output("rev-parse", "HEAD")
        _ = (phase / "phase_baseline").write_text(baseline)
        _ = (phase / "regression-tests").write_text("sample regression\n")
        regression = self.root / "src" / "regression.rs"
        _ = regression.write_text("#[test] fn regression() {}\n")
        self.run_git("add", "-f", str(regression))
        _ = (phase / "regression.patch").write_text(self.git_output("diff", "--binary", "HEAD", "--", str(regression)))
        _ = (self.root / "src" / "lib.rs").write_text("pub fn value() -> u8 { 2 }\n")
        self.run_git("add", "-f", "src/lib.rs")

    def git_output(self, *arguments: str) -> str:
        return subprocess.run(["git", *arguments], cwd=self.root, env=self.environment, text=True,
                              capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS).stdout

    def test_batch_orders_format_package_tests_and_workspace_lint(self) -> None:
        result = self.batch(lint_scope="workspace")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.batch_lines(result), [
            "verify.sh batch format workspace green cached=no",
            "verify.sh batch test sample green cached=no",
            "verify.sh batch test other green cached=no",
            "verify.sh batch lint workspace green cached=no",
            "verify.sh batch result green",
        ])
        stages = [args[0] for args in self.cargo_calls()
                  if args[:2] in (["+nightly", "fmt"], ["nextest", "run"]) and "--version" not in args
                  or args[0] in ("mend", "clippy")]
        self.assertEqual(stages[0:4], ["+nightly", "nextest", "nextest", "mend"])
        self.assertEqual(stages[-1], "clippy")

    def test_batch_runs_all_packages_after_a_failure_and_names_the_failed_package(self) -> None:
        self.environment["TEST_SAMPLE_STATUS"] = "100"
        result = self.batch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("verify.sh batch test sample red cached=no", result.stdout)
        self.assertIn("verify.sh batch test other green cached=no", result.stdout)
        self.assertIn("verify.sh batch lint sample,other green cached=no", result.stdout)

    def test_batch_rejects_a_lint_edit_after_tests(self) -> None:
        self.environment["TEST_EDIT_DURING"] = "mend"
        result = self.batch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("verify.sh batch lint sample,other red cached=no (tests stale)", result.stdout)

    def test_batch_rejects_a_lint_edit_in_a_dirty_submodule(self) -> None:
        module = self.root / "nested"
        module.mkdir()
        self.run_git("-C", str(module), "init", "--quiet")
        source = module / "lib.rs"
        _ = source.write_text("pub fn value() -> u8 { 1 }\n")
        _ = commit_repository(module, "Add submodule source", env=self.environment)
        self.run_git("add", "--force", module.name)
        _ = commit_repository(self.root, "Add submodule", module.name, env=self.environment)
        _ = source.write_text("pub fn value() -> u8 { 2 }\n")
        self.environment.update(TEST_EDIT_DURING="mend", TEST_EDIT_FILE="nested/lib.rs")

        result = self.batch()
        self.assertIn("verify.sh batch lint sample,other red cached=no (tests stale)", result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

        del self.environment["TEST_EDIT_DURING"]
        self.environment["TEST_SAMPLE_STATUS"] = "100"
        result = self.batch()
        self.assertIn("verify.sh batch test sample red cached=no", result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_batch_reports_the_cache_result_of_each_package_and_lint(self) -> None:
        first = self.batch()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self.batch()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("verify.sh batch test sample green cached=yes", second.stdout)
        self.assertIn("verify.sh batch test other green cached=yes", second.stdout)
        self.assertIn("verify.sh batch lint sample,other green cached=yes", second.stdout)
        self.assertNotIn(["run"], [call[:1] for call in self.calls("nextest")])
        self.assertFalse(self.clippy_ran())

    def test_batch_cleanup_finishes_while_the_background_sweep_runs(self) -> None:
        release = self.root / "sweep-release"
        ready = self.root / "sweep-pid"
        self.write_executable(self.root / "bin" / "systemd-cat", f"""
import os
from pathlib import Path
from scripts.direct.wait_for_test_state import wait_for_file

Path({str(ready)!r}).write_text(str(os.getpid()))
wait_for_file(Path({str(release)!r}))
""")
        output = self.root / "batch.out"
        with output.open("wb") as log, subprocess.Popen(
            ["bash", str(support.VERIFY), *self.batch_arguments()], cwd=self.root,
            env={**self.environment, "LINT_SKIP_SWEEP": "0"}, start_new_session=True,
            stdout=log, stderr=subprocess.STDOUT,
        ) as process:
            try:
                sweep = wait_for_pid(ready, processes=(process,))
                wait_until(lambda: "verify.sh batch result green" in output.read_text() or process.poll() is not None,
                           self.root, processes=(process,), watch_writes=True)
                with os.fdopen(os.pidfd_open(sweep), "rb") as sweep_exit:
                    try:
                        status = process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.fail("batch cleanup waited for the background sweep")
                    self.assertEqual(status, 0, output.read_text())
                    self.assertFalse(select.select([sweep_exit], [], [], 0)[0])
                    _ = release.touch()
                    self.assertTrue(select.select([sweep_exit], [], [], TEST_HANG_GUARD_SECS)[0])
                self.assertEqual(list(Path(self.environment["TMPDIR"]).glob("verify-batch.*")), [])
            finally:
                _ = release.touch()
                if process.poll() is None:
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_batch_refuses_a_regression_that_passed_on_the_baseline(self) -> None:
        self.fail_first(status=0)
        result = self.batch("--fail-first")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("verify.sh batch fail-first sample red cached=no (fail-first passed on baseline)", result.stdout)
        self.assertIn("verify.sh batch test sample green cached=no", result.stdout)

    def test_batch_places_baseline_copy_and_all_current_steps_under_one_run(self) -> None:
        self.fail_first(status=100)
        admission = caller_admission(self, self.root, self.environment)
        with patch.object(admission.server, "handle_request", wraps=admission.server.handle_request) as observe:
            result = self.batch("--fail-first")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verify.sh batch fail-first sample green cached=no", result.stdout)
        self.assertTrue((self.root / "batch-copies" / "slots" / "case-slot1").is_dir())
        placements = [request for call in observe.call_args_list
                      if isinstance(request := cast(wire.Request, call.args[0]), wire.PlacementRequest)]
        first_requests = list({request.episode: request for request in reversed(placements)}.values())[::-1]
        self.assertEqual([request.constraints.package for request in first_requests], [
            "sample", "sample", "other", "sample", "sample",
        ])
        self.assertIn("copies-source", first_requests[0].identity.folder)
        self.assertEqual(first_requests[1].identity.folder, str(self.root))
        self.assertIsInstance(first_requests[0].run, wire.RunIdentity)
        self.assertEqual({request.run for request in placements}, {first_requests[0].run})
        self.assertNotEqual(first_requests[0].constraints.source_key, first_requests[1].constraints.source_key)


    def test_batch_stop_releases_the_baseline_grant_and_copy(self) -> None:
        self.fail_first(status=100)
        admission = caller_admission(self, self.root, self.environment)
        with ShortUnixSocket() as listener:
            ready = listener.bind_socket()
            environment = {**self.environment, "TEST_BASELINE_READY": str(ready)}
            listener.listen(1)
            listener.settimeout(TEST_HANG_GUARD_SECS)
            with subprocess.Popen(
                ["bash", str(support.VERIFY), *self.batch_arguments("--fail-first")],
                cwd=self.root, env=environment, start_new_session=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ) as process:
                try:
                    with os.fdopen(os.pidfd_open(process.pid), "rb") as stopped:
                        ready_fds = select.select([listener, stopped], [], [], TEST_HANG_GUARD_SECS)[0]
                    if listener not in ready_fds:
                        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                        self.fail("the baseline test did not start: " + output + errors)
                    with listener.accept()[0] as connection:
                        connection.settimeout(TEST_HANG_GUARD_SECS)
                        with connection.makefile("r") as data:
                            snapshot = Path(data.readline().strip())
                        self.assertTrue(snapshot.is_dir())
                        self.assertTrue(admission.caller_reservations())
                        send_process_group(process.pid, signal.SIGTERM)
                        self.assertEqual(connection.recv(1), b"")
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
                    released = admission.callers_released()
                    snapshot_exists = snapshot.parent.exists()
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertTrue(released, "the batch exited before it released the grant\n" + output + errors)
                    self.assertFalse(snapshot_exists, "the batch exited before it removed the private copy\n" + output + errors)
                finally:
                    if process.poll() is None:
                        send_process_group(process.pid, signal.SIGKILL)
                self.assertEqual(process.returncode, -signal.SIGTERM, output + errors)
                self.assertNotIn("verify.sh batch result green", output)


class BatchCancellationTests(unittest.TestCase):
    def test_baseline_patch_stops_when_its_coordinator_exits(self) -> None:
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        git = scratch / "git"
        pid_file = scratch / "git-pid"
        _ = git.write_text(f"""#!{sys.executable}
import os
from pathlib import Path
import signal

Path({str(pid_file)!r}).write_text(str(os.getpid()))
signal.pause()
""")
        git.chmod(0o755)
        batch = support.VERIFY.with_name("verify_batch.py")
        lock = scratch / "cleanup.lock"
        with lock.open("w+b") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX)
            with subprocess.Popen(
                [sys.executable, str(batch), "--baseline", "verify", "done", "patch", "sample=regression", "--"],
                cwd=scratch, pass_fds=(lease.fileno(),), start_new_session=True,
                env={**os.environ, "PATH": f"{scratch}:{os.environ['PATH']}",
                     "COPIES_STEP_LOCK_FD": str(lease.fileno())},
            ) as process:
                try:
                    git_pid = wait_for_pid(pid_file, processes=(process,))
                    with os.fdopen(os.pidfd_open(git_pid), "rb") as git_exit:
                        process.terminate()
                        self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
                        self.assertTrue(select.select([git_exit], [], [], 5)[0],
                                        "the patch process survived its coordinator")
                finally:
                    if process.poll() is None:
                        process.kill()
                    try:
                        send_process_group(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def run_python(self, program: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-c", program, *arguments], capture_output=True, text=True,
                              check=False, timeout=TEST_HANG_GUARD_SECS)

    def test_a_batch_command_uses_the_callers_process_group(self) -> None:
        result = self.run_python("""
import os, sys
from pathlib import Path
from scripts.direct.verify_batch import BatchCommands
print(os.getpgrp(), flush=True)
raise SystemExit(BatchCommands(Path("unused"), ()).run(
    [sys.executable, "-c", "import os; print(os.getpgrp())"], {}))
""")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        parent, child = result.stdout.splitlines()
        self.assertEqual(parent, child)

    def test_term_during_the_final_tree_check_stops_the_batch(self) -> None:
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        result = self.run_python("""
import os, signal, sys
from pathlib import Path
from unittest.mock import patch
from scripts.direct import verify_batch as batch

checks = iter(("tree", "tree", "final"))

def tree_key(_directory):
    if next(checks) == "final":
        os.kill(os.getpid(), signal.SIGTERM)
    return "tree"

commands = batch.BatchCommands(Path("unused"), ())
with patch.object(commands, "verify_step", return_value=batch.BatchStepResult(0, False)):
    with patch.object(batch.treekey, "tree_key", side_effect=tree_key):
        raise SystemExit(batch.run_batch(commands, Path.cwd(), ("sample",), batch.LintScope.WORKSPACE,
                                        batch.NoBaselineProof.SKIP, Path(sys.argv[1])))
""", str(scratch))
        self.assertEqual(result.returncode, -signal.SIGTERM, result.stdout + result.stderr)
        self.assertNotIn("verify.sh batch result green", result.stdout)

    def test_a_failed_copy_cleanup_names_the_baseline_step(self) -> None:
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        result = self.run_python("""
import sys
from pathlib import Path
from unittest.mock import patch
from scripts.direct import verify_batch as batch

scratch = Path(sys.argv[1])
plan = batch.BaselineProof("commit", scratch / "patch", (batch.RegressionCase("sample", "regression"),))

def run_after_proof(_command, _environment):
    marker = scratch / "baseline-done"
    with patch.object(batch, "run_baseline_tests", return_value=0), patch.object(batch.subprocess, "run") as run:
        run.return_value.returncode = 0
        assert batch.main(["--baseline", "verify", str(marker), str(scratch / "patch"), "sample=regression", "--"]) == 0
    return 2

commands = batch.BatchCommands(Path("verify"), ())
with patch.object(commands, "run", side_effect=run_after_proof):
    assert not batch.run_baseline_copy(commands, scratch, plan, scratch)
""", str(scratch))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verify.sh batch fail-first sample red cached=no (baseline copy exit 2)", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()

"""Test how checkpoint gates enter placement and report the result."""
from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout, suppress
import io
from dataclasses import replace
from functools import partial
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from textwrap import dedent
from typing import cast, final
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.build_queue import (
    admission_client, admission_protocol, admission_records, admission_service, admission_wire, execution_identity,
    first_step, placement,
)
from scripts.build_queue.admission_jobs import StepJob
from scripts.build_queue.admission_test_support import only_admitted
from scripts.build_queue.queue_fixture import write_process
from scripts.build_queue.queue_place import queue_place
from scripts.build_queue.test_placement import PlacementFixture
from scripts.build_targets.target_config import PinnedTarget
from scripts.copies import copies
from scripts.lib import build_folders, process_stat
from scripts.lib.build_steps import GATE_PRELAUNCH_REFUSED_STATUS, GATE_UNSETTLED_LAUNCH_STATUS, gate_job_from_process
from scripts.lint.memory_test_support import write_config_fixture
from scripts.build_hold.owning_session import NoOwningSession
from scripts.mac_test.claim_state import RepositoryScope
from scripts.production import checkpoint_gate, merge_checkpoint
from scripts.production.gate_suites_test_support import GateSuitesFixture
from scripts.production.production_doc import ProductionDocument
from scripts.production.staff_test_support import fixture_member, isolated_staff_file


GATE = ("python3", "scripts/production/gate.py", "--tests-only")


def write_gate(folder: Path, command: Sequence[str], status: int, diagnostic: str = "",
               *, lease_fd: int | None = None) -> None:
    script = folder / command[1]
    script.parent.mkdir(parents=True, exist_ok=True)
    _ = script.write_text(dedent(f"""\
        import os
        import sys

        with os.fdopen(int(os.environ[{copies.PREPARED_GATE_FD!r}]), "rb") as handoff:
            assert handoff.read(32) == f"{{os.getpid()}}\\n".encode("ascii")
        lease_fd = {lease_fd!r}
        if lease_fd is not None:
            assert os.read(lease_fd, 32) == b"gate fixture lease\\n"
        assert sys.argv[1:] == {list(command[2:])!r}
        print("gate fixture started", file=sys.stderr, flush=True)
        sys.stderr.write({diagnostic!r})
        raise SystemExit({status})
        """))


def run_gate_step(case: unittest.TestCase, argv: list[str]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryFile(mode="w+") as errors, redirect_stderr(errors):
        # Capture the gate child and the cleanup messages in the same stream.
        saved_stderr = os.dup(2)
        try:
            _ = os.dup2(errors.fileno(), 2)
            code = copies.main(argv)
        finally:
            _ = os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
        _ = errors.seek(0)
        diagnostic = errors.read()
    case.assertEqual(diagnostic.splitlines().count("gate fixture started"), 1, diagnostic)
    return subprocess.CompletedProcess(argv, code, "", diagnostic)


class CheckpointGateTests(unittest.TestCase):
    def test_merge_gate_uses_the_committed_copy_for_the_initial_run_and_flake_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            production = ProductionDocument(folder / "production.md", "fixture", "merge", folder,
                                            folder / "production.log", ZoneInfo("UTC"))
            prefix = ["env", "HERDR_BIN=/nonexistent/herdr"]
            command = [*prefix, *GATE, "--checkout", "."]
            step = checkpoint_gate.CheckpointTestStep("merge test 1", command, folder)
            log = folder / "test.log"
            script_dir = Path(checkpoint_gate.__file__).resolve().parents[1]
            expected = [*prefix, str(script_dir / "lib/py"), str(script_dir / "copies/copies.py"), "run",
                        "--repo", str(folder), "--commit", "merged-tip", "--", *GATE, "--checkout", "{tree}"]
            retry = [*expected, "--module", "known_flake"]

            with mock.patch.object(subprocess, "run", side_effect=[
                    subprocess.CompletedProcess(expected, 1, RED_OUTPUT, ""),
                    subprocess.CompletedProcess(retry, 0, "", ""),
            ]) as run:
                result = checkpoint_gate.run_test_steps(production, [step], frozenset(), frozenset({"known_flake"}),
                                                        log, merge_head="merged-tip")
            options = {"cwd": folder, "capture_output": True, "text": True, "check": False, "pass_fds": ()}
            self.assertEqual(run.call_args_list, [mock.call(expected, **options), mock.call(retry, **options)])
            self.assertEqual(result, "merge test 1")
            self.assertIn("merge_test_1_EXIT=1", log.read_text())
            self.assertIn("merge_test_1_RERUN_EXIT=0", log.read_text())
            self.assertIn("known flaky tests known_flake", production.log.read_text())

    def test_named_gate_uses_a_supervisor_with_the_gate_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryFile() as lease:
            folder = Path(temporary)
            _ = lease.write(b"gate fixture lease\n")
            _ = lease.seek(0)
            descriptor = lease.fileno()
            token = copies.COMMAND_LOCKS.set((descriptor,))
            self.addCleanup(copies.COMMAND_LOCKS.reset, token)
            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "natedev,mac-linux"}),
                  mock.patch.object(checkpoint_gate, "run_gate_step_command",
                      return_value=subprocess.CompletedProcess(GATE, 0, "", "")) as run):
                result = checkpoint_gate.run_test_command(list(GATE), folder)
                self.assertEqual(result.returncode, 0)
                run.assert_called_once()
                argv = cast(list[str], run.call_args.args[0])
                self.assertEqual(run.call_args.args[1:], (folder, (descriptor,)))
                self.assertEqual(argv[2:], ["gate-step", "--repo", str(folder), "--targets", "natedev,mac-linux",
                    "--lease-fd", str(descriptor), "--", *GATE])

            process = folder / "42"
            write_process(folder, 42, start="123")
            _ = (process / "cmdline").write_text("\0".join(("python3", *argv[1:])) + "\0")
            self.assertEqual(gate_job_from_process("gate", process, start_time="123"), StepJob("test-all"))

            write_gate(folder, GATE, 0, lease_fd=descriptor)
            with mock.patch.object(first_step, "admitted_run") as place:
                handoff = mock.Mock()
                admitted = mock.Mock(spec=first_step.FirstStepAdmission, handoff=handoff,
                                     grant=first_step.HandedOff())
                place.return_value = nullcontext(admitted)
                result = run_gate_step(self, argv[2:])
                self.assertEqual(result.returncode, 0, result.stderr)
                place.assert_called_once_with(list(GATE), folder, targets=("natedev", "mac-linux"))
                handoff.assert_called_once_with(folder)

    def test_gate_without_targets_starts_directly(self) -> None:
        folder = Path("/fixture")
        with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}),
              mock.patch.object(build_folders, "repo_word", return_value="hana"),
              mock.patch.object(subprocess, "run",
                  return_value=subprocess.CompletedProcess(GATE, 0, "", "")) as run):
            _ = checkpoint_gate.run_test_command(list(GATE), folder)
        run.assert_called_once_with(list(GATE), cwd=folder, capture_output=True, text=True, check=False, pass_fds=())

    def test_gate_step_arguments_do_not_select_a_supervisor(self) -> None:
        folder = Path("/fixture")
        command = ["/bin/echo", str(Path(copies.__file__).resolve()), "gate-step"]
        completed = subprocess.CompletedProcess(command, 0, "echo output\n", "")
        with (mock.patch.object(checkpoint_gate, "run_gate_step_command") as supervise,
              mock.patch.object(subprocess, "run", return_value=completed) as run):
            self.assertIs(checkpoint_gate.run_test_command(command, folder), completed)
        supervise.assert_not_called()
        run.assert_called_once_with(command, cwd=folder, capture_output=True, text=True, check=False, pass_fds=())

    def test_supervisor_signal_error_preserves_the_first_interrupt(self) -> None:
        interrupt = KeyboardInterrupt("first interrupt")
        communicate = mock.Mock(side_effect=[interrupt, ("", "")])
        send_signal = mock.Mock(side_effect=ProcessLookupError("supervisor exited"))
        child = mock.Mock(communicate=communicate, send_signal=send_signal)
        with (mock.patch.object(subprocess, "Popen", return_value=nullcontext(child)),
              self.assertRaises(KeyboardInterrupt) as raised):
            _ = checkpoint_gate.run_gate_step_command(list(GATE), Path("/fixture"), ())
        send_signal.assert_called_once_with(signal.SIGINT)
        self.assertEqual(communicate.call_count, 2)
        self.assertIs(raised.exception, interrupt)

    def test_supervisor_wait_survives_further_interrupts(self) -> None:
        interrupt = KeyboardInterrupt("first interrupt")
        communicate = mock.Mock(side_effect=[interrupt, KeyboardInterrupt(), KeyboardInterrupt(), ("", "")])
        send_signal = mock.Mock()
        child = mock.Mock(communicate=communicate, send_signal=send_signal)
        with (mock.patch.object(subprocess, "Popen", return_value=nullcontext(child)),
              self.assertRaises(KeyboardInterrupt) as raised):
            _ = checkpoint_gate.run_gate_step_command(list(GATE), Path("/fixture"), ())
        send_signal.assert_called_once_with(signal.SIGINT)
        self.assertEqual(communicate.call_count, 4)
        self.assertIs(raised.exception, interrupt)

    def test_supervisor_waits_after_cleanup_output_fails(self) -> None:
        for error in (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"), OSError("read failed")):
            with self.subTest(error=error):
                interrupt = KeyboardInterrupt("first interrupt")
                communicate = mock.Mock(side_effect=[interrupt, error])
                send_signal = mock.Mock()
                wait = mock.Mock(side_effect=[KeyboardInterrupt(), 130])
                stdout, stderr = mock.Mock(), mock.Mock()
                child = mock.Mock(communicate=communicate, send_signal=send_signal, wait=wait,
                                  stdout=stdout, stderr=stderr)
                with (mock.patch.object(subprocess, "Popen", return_value=nullcontext(child)),
                      self.assertRaises(KeyboardInterrupt) as raised):
                    _ = checkpoint_gate.run_gate_step_command(list(GATE), Path("/fixture"), ())
                self.assertEqual(child.mock_calls[:6], [
                    ("communicate", (), {}),
                    ("send_signal", (signal.SIGINT,), {}),
                    ("communicate", (), {}),
                    ("stdout.close", (), {}),
                    ("stderr.close", (), {}),
                    ("wait", (), {}),
                ])
                send_signal.assert_called_once_with(signal.SIGINT)
                self.assertEqual(communicate.call_count, 2)
                self.assertEqual(wait.call_count, 2)
                self.assertIs(raised.exception, interrupt)

    def test_supervisor_retries_only_an_interrupted_signal_send(self) -> None:
        interrupt = KeyboardInterrupt("first interrupt")
        communicate = mock.Mock(side_effect=[interrupt, KeyboardInterrupt(), ("", "")])
        send_signal = mock.Mock(side_effect=[KeyboardInterrupt("before delivery"), None])
        child = mock.Mock(communicate=communicate, send_signal=send_signal)
        with (mock.patch.object(subprocess, "Popen", return_value=nullcontext(child)),
              self.assertRaises(KeyboardInterrupt) as raised):
            _ = checkpoint_gate.run_gate_step_command(list(GATE), Path("/fixture"), ())
        self.assertEqual(child.mock_calls, [
            ("communicate", (), {}),
            ("send_signal", (signal.SIGINT,), {}),
            ("send_signal", (signal.SIGINT,), {}),
            ("communicate", (), {}),
            ("communicate", (), {}),
        ])
        self.assertIs(raised.exception, interrupt)

    def test_claude_gate_without_target_override_launches_with_default_targets(self) -> None:
        with PlacementFixture() as fixture:
            _ = write_config_fixture(fixture.root / "build_targets.conf", "targets=natedev\nenabled.natedev=on\n")
            _ = write_config_fixture(fixture.root / "cloud_gate.conf", "enabled=off\n")
            with (mock.patch.dict(os.environ),
                  mock.patch.object(build_folders, "repo_word", return_value="claude"),
                  mock.patch.object(checkpoint_gate, "run_gate_step_command",
                      return_value=subprocess.CompletedProcess(GATE, 0, "", "")) as run):
                _ = os.environ.pop("BUILDLOG_GATE_TARGETS", None)
                result = checkpoint_gate.run_test_command(list(GATE), fixture.root)
            self.assertEqual(result.returncode, 0)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][2:],
                             ["gate-step", "--repo", str(fixture.root), "--targets", "natedev", "--", *GATE])

    def test_named_module_gate_keeps_the_parent_reservation(self) -> None:
        with PlacementFixture() as fixture:
            outer = fixture.request(owner=fixture.fixture_owner(700001, "123"))
            with fixture.loop_turn():
                grant = only_admitted(fixture.server.handle_request(outer))
            fixture.hold_starts()
            reservation = f"{grant.reservation}\t{outer.owner.pid}\t{fixture.state_dir}\t{fixture.socket_path}"
            command = [*GATE, "--module", "lib/test_infrastructure.py"]
            _ = write_config_fixture(fixture.root / "build_targets.conf", "targets=natedev\n")

            def read_private_stat(path: Path) -> str:
                return (fixture.proc.path / path.relative_to("/proc")).read_text()

            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "", "BUILDLOG_RESERVED_STEP": reservation}),
                  mock.patch.object(os, "getppid", return_value=outer.owner.pid),
                  mock.patch.object(admission_records, "read_proc_text", side_effect=read_private_stat),
                  mock.patch.object(process_stat, "process_fields",
                      side_effect=partial(process_stat.process_fields, proc_root=fixture.proc.path)),
                  mock.patch.object(build_folders, "repo_word", return_value="claude"),
                  mock.patch.object(subprocess, "run",
                      return_value=subprocess.CompletedProcess(command, 0, "", "")) as run):
                self.assertEqual(admission_client.outer_reservation(),
                                 admission_client.OuterReservation(grant.reservation, outer.owner.pid))
                result = checkpoint_gate.run_test_command(command, fixture.root)
            self.assertEqual(result.returncode, 0)
            run.assert_called_once_with(command, cwd=fixture.root, capture_output=True, text=True, check=False,
                                        pass_fds=())

    def test_claude_gate_without_enabled_targets_refuses_before_start(self) -> None:
        with PlacementFixture() as fixture:
            _ = write_config_fixture(fixture.root / "build_targets.conf",
                "targets=mac-linux,natedev\nenabled.mac-linux=off\nenabled.natedev=off\n")
            _ = write_config_fixture(fixture.root / "cloud_gate.conf", "enabled=off\n")
            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}),
                  mock.patch.object(build_folders, "repo_word", return_value="claude"),
                  mock.patch.object(subprocess, "run",
                      return_value=subprocess.CompletedProcess(GATE, 0, "", "")) as run):
                with self.assertRaises(checkpoint_gate.Stop) as refused:
                    _ = checkpoint_gate.run_test_command(list(GATE), fixture.root)
            run.assert_not_called()
            self.assertEqual(refused.exception.state, "blocked")
            self.assertEqual(refused.exception.detail,
                             "No enabled build target can run this Claude gate. Check enabled.* in build_targets.conf.")


@final
class PackageStepFixture:
    def __init__(self, case: unittest.TestCase) -> None:
        root = Path(case.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.production = ProductionDocument(root / "production.md", "fixture", "merge", root,
                                             root / "production.log", ZoneInfo("UTC"))
        self.log = root / "test.log"
        self.verify = str(root / "verify.sh")
        _ = (root / "Cargo.toml").write_text(
            '[workspace]\nresolver = "2"\nmembers = ["crates/*"]\nexclude = ["crates/solo", "vendor/wesl"]\n')
        self.paths = tuple(self.write_package(f"crates/{name}") for name in ("alpha", "beta", "gamma"))
        self.steps = self.select_steps()
        self.joined = ["bash", self.verify, "test", "alpha", "+", "test", "beta", "+", "test", "gamma"]
        self.beta_red = subprocess.CompletedProcess(self.joined, 1, "  FAIL [  0.1s] (1/1) beta unit_red\n",
            "verify.sh: step result: test alpha exit=0\nverify.sh: step result: test beta exit=1\n")

    def write_package(self, path: str) -> str:
        folder = self.production.checkout / path
        (folder / "src").mkdir(parents=True)
        _ = (folder / "Cargo.toml").write_text(f'[package]\nname = "{folder.name}"\nversion = "0.1.0"\n')
        (folder / "src/lib.rs").touch()
        return f"{path}/src/lib.rs"

    def select_steps(self, *paths: str) -> list[checkpoint_gate.CheckpointTestStep]:
        with mock.patch.object(checkpoint_gate, "tool", return_value=self.verify):
            return checkpoint_gate.test_steps(self.production, (*self.paths, *paths), ())


class CheckpointPackageTests(unittest.TestCase):
    def test_three_packages_use_one_verify_step_list(self) -> None:
        fixture = PackageStepFixture(self)
        rows = "".join(f"verify.sh: step result: test {name} exit=0\n" for name in ("alpha", "beta", "gamma"))
        with mock.patch.object(subprocess, "run",
                               return_value=subprocess.CompletedProcess(fixture.joined, 0, "", rows)) as run:
            result = checkpoint_gate.run_test_steps(fixture.production, fixture.steps, frozenset(), frozenset(),
                                                    fixture.log, merge_head="merged-tip")
        run.assert_called_once_with(fixture.joined, cwd=fixture.production.checkout, capture_output=True, text=True,
                                    check=False, pass_fds=())
        self.assertEqual(result, "package alpha, package beta, package gamma")
        self.assertIn(rows, fixture.log.read_text())

    def test_member_with_cargo_config_runs_separately_from_its_siblings(self) -> None:
        for filename in ("config.toml", "config"):
            with self.subTest(filename=filename):
                fixture = PackageStepFixture(self)
                root = fixture.production.checkout
                beta = root / "crates/beta"
                (root / ".cargo").mkdir()
                (root / ".cargo/config.toml").touch()
                (beta / ".cargo").mkdir()
                (beta / ".cargo" / filename).touch()

                with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                    _ = checkpoint_gate.run_test_steps(fixture.production, fixture.steps, frozenset(), frozenset(), fixture.log)

                self.assertEqual(run.call_args_list, [
                    mock.call(["bash", fixture.verify, "test", "alpha", "+", "test", "gamma"], cwd=root,
                              capture_output=True, text=True, check=False, pass_fds=()),
                    mock.call(["bash", fixture.verify, "test", "beta"], cwd=beta, capture_output=True, text=True,
                              check=False, pass_fds=()),
                ])

    def test_vendored_workspace_uses_its_own_verify_call(self) -> None:
        fixture = PackageStepFixture(self)
        root = fixture.production.checkout
        vendor = root / "vendor" / "wesl"
        path = fixture.write_package("vendor/wesl/crates/wesl")
        _ = (vendor / "Cargo.toml").write_text('[workspace]\nresolver = "2"\nmembers = ["crates/wesl"]\n')

        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = checkpoint_gate.run_test_steps(fixture.production, fixture.select_steps(path),
                                                    frozenset(), frozenset(), fixture.log)

        self.assertEqual(run.call_args_list, [
            mock.call(fixture.joined, cwd=root, capture_output=True, text=True, check=False, pass_fds=()),
            mock.call(["bash", fixture.verify, "test", "wesl"], cwd=vendor, capture_output=True, text=True,
                      check=False, pass_fds=()),
        ])
        self.assertEqual(result, "package alpha, package beta, package gamma, package wesl")

    def test_excluded_package_runs_from_its_own_folder(self) -> None:
        fixture = PackageStepFixture(self)
        root = fixture.production.checkout
        path = fixture.write_package("crates/solo")
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            _ = checkpoint_gate.run_test_steps(fixture.production, fixture.select_steps(path),
                                               frozenset(), frozenset(), fixture.log)
        self.assertEqual(run.call_args_list, [
            mock.call(fixture.joined, cwd=root, capture_output=True, text=True, check=False, pass_fds=()),
            mock.call(["bash", fixture.verify, "test", "solo"], cwd=root / "crates/solo", capture_output=True,
                      text=True, check=False, pass_fds=()),
        ])

    def test_red_step_list_names_the_failed_package(self) -> None:
        fixture = PackageStepFixture(self)
        with (mock.patch.object(subprocess, "run", return_value=fixture.beta_red) as run,
              self.assertRaises(checkpoint_gate.RedCheckpointTests) as red):
            _ = checkpoint_gate.run_test_steps(fixture.production, fixture.steps, frozenset(), frozenset(), fixture.log,
                                               retry_failed_steps=False)
        run.assert_called_once()
        self.assertEqual(red.exception.red, (checkpoint_gate.RedStep(fixture.steps[1], frozenset({"unit_red"})),))
        self.assertIn("package beta (unit_red) failed", red.exception.detail)

    def test_step_list_runs_remaining_packages_and_retries_the_flake(self) -> None:
        fixture = PackageStepFixture(self)
        beta, gamma = fixture.steps[1].command, fixture.steps[2].command
        with mock.patch.object(subprocess, "run", side_effect=[
            fixture.beta_red, subprocess.CompletedProcess(gamma, 0, "", ""), subprocess.CompletedProcess(beta, 0, "", ""),
        ]) as run:
            result = checkpoint_gate.run_test_steps(fixture.production, fixture.steps, frozenset({"beta"}),
                                                    frozenset(), fixture.log)
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [fixture.joined, gamma, beta])
        self.assertEqual([call.kwargs["cwd"] for call in run.call_args_list],
                         [fixture.production.checkout, fixture.production.checkout, fixture.steps[1].folder])
        self.assertEqual(result, "package alpha, package beta, package gamma")
        self.assertIn("known flake beta", fixture.production.log.read_text())


COMMAND = ["python3", "gate.py", "--tests-only"]


REASON = "mac_linux_no_answer"
RED_OUTPUT = "  FAIL [  0.1s] (1/1) check known_flake\n--- lib/test_infrastructure.py exit=1 ---\n"


class BatchProbeTests(GateSuitesFixture):
    def test_configured_merge_commands_probe_on_natedev_only(self) -> None:
        self.check_configured_merge_commands(under_parent=False)

    def test_configured_merge_commands_probe_under_the_parent_reservation(self) -> None:
        self.check_configured_merge_commands(under_parent=True)

    def probe_environment(self) -> dict[str, str]:
        with isolated_staff_file(Path(self.environment["STAFF_FILE"]), self.environment):
            owner = fixture_member("showrunner", name="probe-owner")
        environment = {**self.environment, "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": owner.current_session,
                       "BUILDLOG_GATE_TARGETS": "natedev,mac-linux"}
        for key in (admission_client.RESERVED_STEP_ENV, "BUILDLOG_PLACEMENT_FILE", "BUILDLOG_PLACEMENT_EPISODE"):
            _ = environment.pop(key, None)
        _ = write_config_fixture(Path(environment["CONF_DIR"]) / "build_targets.conf",
                                 "targets=natedev,mac-linux\n")
        return environment

    def make_probe_batch(self, red: checkpoint_gate.RedStep, pinned_main: str) -> merge_checkpoint.BatchRed:
        log = self.state / "probe.log"
        production = ProductionDocument(self.quiet_production, "fixture", "integration", self.checkout,
                                        log, ZoneInfo("UTC"))
        record = merge_checkpoint.BatchResumeRecord(self.state / "batch", (), ())
        rollback = merge_checkpoint.ResetBatchCheckout(production, self.base, self.base, self.base, record)
        failure = checkpoint_gate.RedCheckpointTests("red fixture", "red fixture", (red,))
        return merge_checkpoint.BatchRed(failure, (), rollback, pinned_main, log, {})

    def test_parent_only_interrupt_reaps_the_probe_gate_and_releases_the_copy(self) -> None:
        pid_file = self.state / "probe-gate-pid"
        self.write(self.main / GATE[1], dedent(f"""\
            import os
            from pathlib import Path
            import signal

            with os.fdopen(int(os.environ[{copies.PREPARED_GATE_FD!r}]), "rb") as handoff:
                assert handoff.read(32) == f"{{os.getpid()}}\\n".encode("ascii")
            Path({str(pid_file)!r}).write_text(str(os.getpid()))
            os.kill({os.getpid()}, signal.SIGINT)
            signal.pause()
            """))
        pinned_main = self.commit(self.main, "interrupt fixture on pinned main")
        step = checkpoint_gate.CheckpointTestStep("merge test 1", list(GATE), self.checkout)
        red = checkpoint_gate.RedStep(step, frozenset({"hooks/test_first.py"}))
        batch = self.make_probe_batch(red, pinned_main)
        with mock.patch.dict(os.environ, self.probe_environment(), clear=True):
            try:
                with self.assertRaises(KeyboardInterrupt):
                    _ = merge_checkpoint.run_batch_probe(batch)
                self.assertEqual(list(build_folders.held_folder_leases()), [])
                self.assert_process_stopped(int(pid_file.read_text()))
                self.assert_gate_released()
            finally:
                if pid_file.exists():
                    with suppress(ProcessLookupError):
                        descriptor = os.pidfd_open(int(pid_file.read_text()))
                        try:
                            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                        finally:
                            os.close(descriptor)

    def check_configured_merge_commands(self, *, under_parent: bool) -> None:
        configured = (
            "env HERDR_BIN=/nonexistent/herdr python3 ~/.claude/scripts/production/merge_gate.py",
            "env HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py ~/.claude/scripts/production/gate.py",
            "~/.claude/scripts/lib/py ~/.claude/scripts/production/gate.py",
        )
        install = Path(self.environment["HOME"]) / ".claude"
        install.mkdir()
        (install / "scripts").symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
        module = "hooks/test_first.py"
        fixture_module = self.main / "scripts" / module
        with fixture_module.open("a", encoding="utf-8") as source:
            _ = source.write(dedent("""\
                row = Path.cwd() / "unit.txt"
                (state / "probe-input").write_text(row.read_text() if row.exists() else "base")
                """))
        pinned_main = self.commit(self.main, "probe fixture on pinned main")
        observed = self.state / "probe-input"
        admission = self.admission.server
        previous = admission.reply_filter
        handle_request = admission.handle_request
        requests: list[admission_wire.PlacementRequest] = []
        grants: list[admission_protocol.Reply] = []

        def observe_request(
            request: admission_protocol.Request,
            connection: admission_service.Connection | admission_service.NoConnection = admission_service.NO_CONNECTION,
        ) -> list[admission_protocol.Reply]:
            if isinstance(request, admission_wire.PlacementRequest):
                requests.append(request)
            return handle_request(request, connection)

        def observe(reply: admission_protocol.Reply) -> admission_protocol.Reply:
            if isinstance(reply, (admission_protocol.Admitted, admission_protocol.LocalPlacementGranted,
                                  admission_protocol.Assignment)):
                grants.append(reply)
            return previous(reply)

        with (mock.patch.dict(os.environ, self.probe_environment(), clear=True),
              mock.patch.object(admission, "reply_filter", side_effect=observe),
              mock.patch.object(admission, "handle_request", side_effect=observe_request),
              ExitStack() as reservation):
            if under_parent:
                parent = reservation.enter_context(queue_place("gate", self.checkout))
                _ = cast(object, reservation.enter_context(
                    mock.patch.dict(os.environ, parent.child_environment(os.environ))))
                self.assertEqual(len(grants), 1)
                self.assertIsInstance(grants[0], admission_protocol.Admitted)
                grants.clear()
            for prefix in configured:
                field = f"- **Merge tests:** `{prefix} --checkout . --tests-only`"
                command, = merge_checkpoint.parse_merge_tests([field], self.checkout)
                step = checkpoint_gate.CheckpointTestStep("merge test 1", list(command), self.checkout)
                red = checkpoint_gate.RedStep(step, frozenset({module}))
                probe = checkpoint_gate.batch_module_command(red, self.checkout, self.checkout)
                self.assertEqual(probe, [*command, "--module", module])
                batch = self.make_probe_batch(red, pinned_main)
                for row, expected in (("", "base"), (self.checkpoint, "checkpoint\n")):
                    with self.subTest(prefix=prefix, row=row):
                        observed.unlink(missing_ok=True)
                        result = merge_checkpoint.run_batch_probe(batch, row)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(observed.read_text(), expected)
                        if under_parent:
                            self.assertEqual(requests, [])
                            self.assertEqual(grants, [])
                        else:
                            self.assertTrue(requests)
                            for request in requests:
                                self.assertEqual(request.route, PinnedTarget("natedev"))
                                self.assertEqual([candidate.destination.target for candidate in request.constraints.candidates],
                                                 ["natedev"])
                            self.assertEqual(len(grants), 1)
                            grant = grants[0]
                            self.assertIsInstance(grant, admission_wire.LocalPlacementGranted)
                            assert isinstance(grant, admission_wire.LocalPlacementGranted)
                            self.assertEqual(grant.assignment.destination.target, "natedev")
                            self.assert_gate_released()
                            requests.clear()
                            grants.clear()
                        self.assertEqual(list(build_folders.held_folder_leases()), [])
            slot, = build_folders.numbered_copy_records()
            self.assertIsInstance(slot, build_folders.NumberedCopySource)
            assert isinstance(slot, build_folders.NumberedCopySource)
            self.assertEqual(slot.commit, pinned_main)
            self.assertEqual(slot.source, self.checkout)
        self.assert_gate_released()
        log = self.state / "probe.log"
        self.assertEqual(log.read_text().count("PROBE_EXIT=0\n"), 6)
        self.assertEqual([event["module"] for event in self.module_events() if event["event"] == "start"],
                         ["test_first.py"] * 6)


@final
class GatePlacementFixture:
    def __init__(self, case: unittest.TestCase) -> None:
        self.case = case
        root = Path(case.enterContext(tempfile.TemporaryDirectory()))
        self.production = ProductionDocument(root / "production.md", "fixture", "merge", root,
                                             root / "production.log", ZoneInfo("UTC"))
        self.log = root / "test.log"
        self.assignment_file = root / "assignment.json"
        self.report_file = self.assignment_file.with_suffix(".json.claim-failure")
        self.step = checkpoint_gate.CheckpointTestStep("merge test 1", COMMAND, root)
        scope = RepositoryScope("fixture")
        guest = placement.PlacementDestination("ssh", "mac-linux", "fixture-mac", scope)
        local = placement.PlacementDestination("local", "natedev", "fixture-dev", scope)
        owner = placement.SubmittingOwner("fixture-dev", 123, "456")
        source = execution_identity.SourceKeyKnown("fixture-source")
        session = NoOwningSession()
        self.guest = placement.PlacementAssignment("episode", "guest-attempt", guest, source, owner, session, 12.5)
        local_assignment = replace(self.guest, destination=local, attempt="local-attempt")
        self.local = admission_wire.LocalPlacementGranted(local_assignment,
            admission_wire.Admitted("reservation", 1, "fixture", "", "", {}))
        self.request = admission_wire.PlacementRequest(self.guest.episode, owner, self.guest.since,
            admission_records.ProgramProfile("gate", str(root), str(root),
                admission_records.HistoryPeakSizing(1, admission_records.FirstProgramPeak(1), 0.0)),
            placement.PlacementConstraints(source, tuple(COMMAND), str(root), "", session,
                (placement.PlacementCandidate(guest), placement.PlacementCandidate(local))))
        self.refusal = admission_wire.ClaimFailure("failure-id", self.guest, REASON)
        admission_wire.write_assignment(self.assignment_file, admission_wire.Assignment(self.guest))
        self.report_failure = case.enterContext(mock.patch.object(admission_client, "claim_failure",
            return_value=admission_wire.DispatchStatus(self.guest, placement.ClosedAssignment(REASON),
                placement.PermissionAvailable(), retry=placement.RetryAt(13.0))))
        self.place_again = mock.Mock(side_effect=self.install_local_assignment)

    def install_local_assignment(self, request: admission_wire.PlacementRequest) -> None:
        self.case.assertEqual(request, self.request)
        self.report_failure.assert_called_once_with(self.refusal)
        admission_wire.write_assignment(self.assignment_file, self.local)

    def run_step(self, child: mock.Mock, *, retry_failed_steps: bool = False) -> str:
        def run(command: list[str], folder: Path, *, merge_head: str) -> subprocess.CompletedProcess[str]:
            self.case.assertEqual(merge_head, "")
            return first_step.run_gate_with_placement(
                lambda: cast("subprocess.CompletedProcess[str]", child(command, folder)),
                self.assignment_file, self.request, self.place_again)

        with mock.patch.object(checkpoint_gate, "run_test_command", side_effect=run):
            return checkpoint_gate.run_test_steps(self.production, [self.step], frozenset(),
                frozenset({"known_flake"}), self.log, retry_failed_steps=retry_failed_steps)

    def refuse_guest(self) -> subprocess.CompletedProcess[str]:
        _ = self.report_file.write_text(admission_wire.encode_request(self.refusal))
        return subprocess.CompletedProcess(COMMAND, 79, RED_OUTPUT, "")

    def run_gate_step(self, command: list[str], folder: Path, *, merge_head: str) -> subprocess.CompletedProcess[str]:
        self.case.assertEqual(merge_head, "")
        return run_gate_step(self.case, ["gate-step", "--repo", str(folder), "--", *command])


@final
class CheckpointGatePlacementTests(unittest.TestCase):
    def check_exit_status(self, status: int, *, retry_failed_steps: bool, output: str = RED_OUTPUT) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        production = ProductionDocument(root / "production.md", "fixture", "merge", root,
                                        root / "production.log", ZoneInfo("UTC"))
        log = root / "test.log"
        command = ["env", "HERDR_BIN=/nonexistent/herdr", *GATE]
        step = checkpoint_gate.CheckpointTestStep("merge test 1", command, root)

        with (mock.patch.object(subprocess, "run",
                                return_value=subprocess.CompletedProcess(command, status, output, "")) as child,
              self.assertRaises(checkpoint_gate.Stop) as stopped):
            _ = checkpoint_gate.run_test_steps(production, [step], frozenset(), frozenset({"known_flake"}), log,
                                               retry_failed_steps=retry_failed_steps, merge_head="merged-tip")
        child.assert_called_once()
        if status in (0, 1):
            assert isinstance(stopped.exception, checkpoint_gate.RedCheckpointTests)
            self.assertEqual(stopped.exception.red, (checkpoint_gate.RedStep(step, frozenset({"lib/test_infrastructure.py"})),))
        else:
            self.assertEqual(stopped.exception.state, "blocked")
            self.assertIn(f"status {status}", stopped.exception.detail)
            self.assertIn(str(log), stopped.exception.detail)
            logged = log.read_text()
            self.assertIn(f"merge_test_1_EXIT={status}", logged)
            self.assertNotIn("RERUN", logged)
            self.assertNotIn("BASE", logged)
        self.assertFalse(production.log.exists())

    def test_silent_guest_requests_placement_again_at_original_queue_time(self) -> None:
        fixture = GatePlacementFixture(self)

        def run(command: list[str], folder: Path) -> subprocess.CompletedProcess[str]:
            self.assertEqual((command, folder), (COMMAND, fixture.step.folder))
            if admission_wire.read_assignment(fixture.assignment_file).assignment == fixture.guest:
                return fixture.refuse_guest()
            self.assertEqual(admission_wire.read_assignment(fixture.assignment_file), fixture.local)
            return subprocess.CompletedProcess(command, 0, "", "")

        invoked = mock.Mock(side_effect=run)
        with (mock.patch.object(checkpoint_gate, "report", wraps=checkpoint_gate.report) as report,
              redirect_stdout(io.StringIO()) as output):
            detail = fixture.run_step(invoked)
        self.assertEqual(detail, fixture.step.label)
        self.assertEqual(invoked.call_count, 2)
        fixture.report_failure.assert_called_once_with(fixture.refusal)
        fixture.place_again.assert_called_once_with(fixture.request)
        # gate-step prints the retry line into the gate output.
        self.assertIn("placement: retry — fixture-mac (mac-linux) did not answer.", output.getvalue())
        self.assertIn("0 red", str(report.call_args_list))

    def test_silent_guest_after_its_consume_requests_placement_again(self) -> None:
        # The guest gate consumed its launch permission, then took no run.
        fixture = GatePlacementFixture(self)
        fixture.report_failure.return_value = admission_wire.DispatchStatus(fixture.guest,
            placement.CurrentAssignment(), placement.PermissionConsumed(12.0), retry=placement.RetryAt(13.0))
        run = mock.Mock(side_effect=[fixture.refuse_guest(), subprocess.CompletedProcess(COMMAND, 0, "", "")])
        with redirect_stdout(io.StringIO()) as output:
            def replace_assignment(request: admission_wire.PlacementRequest) -> None:
                self.assertIn(f"waiting for Mac Linux to confirm the end of attempt {fixture.guest.attempt}\n",
                              output.getvalue())
                fixture.install_local_assignment(request)

            fixture.place_again.side_effect = replace_assignment
            detail = fixture.run_step(run)
        self.assertEqual(detail, fixture.step.label)
        self.assertEqual(run.call_count, 2)
        fixture.place_again.assert_called_once_with(fixture.request)

    def test_silent_guest_blocks_the_checkpoint_without_a_retry(self) -> None:
        self.check_exit_status(GATE_PRELAUNCH_REFUSED_STATUS, retry_failed_steps=True)

    def test_unknown_guest_result_blocks_the_checkpoint_without_a_retry(self) -> None:
        self.check_exit_status(GATE_UNSETTLED_LAUNCH_STATUS, retry_failed_steps=True)

    def test_silent_guest_output_does_not_select_a_flaky_test(self) -> None:
        fixture = GatePlacementFixture(self)
        run = mock.Mock(side_effect=[fixture.refuse_guest(), subprocess.CompletedProcess(COMMAND, 0, "", "")])
        detail = fixture.run_step(run, retry_failed_steps=True)
        self.assertEqual(detail, fixture.step.label)
        self.assertEqual(run.call_args_list, [mock.call(COMMAND, fixture.step.folder)] * 2)
        self.assertFalse(fixture.production.log.exists())
        self.assertNotIn("RERUN", fixture.log.read_text())

    def test_red_after_launch_keeps_only_the_real_red_module(self) -> None:
        fixture = GatePlacementFixture(self)
        red = "lib/test_gate.py"
        run = mock.Mock(side_effect=[fixture.refuse_guest(), subprocess.CompletedProcess(COMMAND, 1, f"--- {red} exit=1 ---\n", "")])
        with self.assertRaises(checkpoint_gate.RedCheckpointTests) as stopped:
            _ = fixture.run_step(run)
        self.assertEqual(stopped.exception.red, (checkpoint_gate.RedStep(fixture.step, frozenset({red})),))
        self.assertEqual(run.call_count, 2)
        fixture.report_failure.assert_called_once_with(fixture.refusal)

    def test_exit_80_blocks_checkpoint_without_rerun(self) -> None:
        fixture = GatePlacementFixture(self)
        result = subprocess.CompletedProcess(COMMAND, GATE_UNSETTLED_LAUNCH_STATUS, "", "settlement pending")
        with (mock.patch.object(checkpoint_gate, "run_test_command", return_value=result) as run,
              self.assertRaises(checkpoint_gate.Stop) as stopped):
            _ = checkpoint_gate.run_test_steps(fixture.production, [fixture.step], frozenset(),
                                               frozenset(), fixture.log)
        self.assertEqual(stopped.exception.state, "blocked")
        self.assertIn("settlement", stopped.exception.detail)
        run.assert_called_once_with(COMMAND, fixture.step.folder, merge_head="")

    def test_queue_loss_during_replacement_preserves_exit_79(self) -> None:
        fixture = GatePlacementFixture(self)
        _ = fixture.refuse_guest()
        write_gate(fixture.step.folder, COMMAND, GATE_PRELAUNCH_REFUSED_STATUS)

        @contextmanager
        def tool_folder(_tool: str) -> Generator[Path]:
            try:
                yield fixture.step.folder
            finally:
                raise OSError("fixture folder cleanup failed")

        def run(command: list[str], folder: Path, *, merge_head: str) -> subprocess.CompletedProcess[str]:
            result = fixture.run_gate_step(command, folder, merge_head=merge_head)
            self.assertEqual(result.returncode, GATE_PRELAUNCH_REFUSED_STATUS, result.stderr)
            return result

        with (PlacementFixture(), mock.patch.dict(os.environ),
              mock.patch.object(first_step, "gate_targets", return_value=("mac-linux",)),
              mock.patch.object(first_step, "_gate_request", return_value=fixture.request),
              mock.patch.object(admission_records, "process_start_time", return_value="456"),
              mock.patch.object(build_folders, "tool_folder", side_effect=tool_folder),
              mock.patch.object(admission_client, "_connect", side_effect=FileNotFoundError("fixture socket is gone")),
              mock.patch.object(admission_client, "acquire_placement",
                  return_value=admission_wire.Assignment(fixture.guest)) as acquire,
              mock.patch.object(checkpoint_gate, "run_test_command", side_effect=run) as runner,
              redirect_stdout(io.StringIO())):
            _ = os.environ.pop("BUILDLOG_PLACEMENT_FILE", None)
            with self.assertRaises(checkpoint_gate.Stop) as stopped:
                _ = checkpoint_gate.run_test_steps(fixture.production, [fixture.step], frozenset(), frozenset(), fixture.log)
        self.assertEqual(stopped.exception.state, "blocked")
        fixture.report_failure.assert_not_called()
        acquire.assert_called_once_with(fixture.request)
        runner.assert_called_once_with(COMMAND, fixture.step.folder, merge_head="")
        output = fixture.log.read_text()
        self.assertIn("Gate cleanup failed. no socket", output)
        self.assertIn("Gate cleanup failed. fixture folder cleanup failed", output)
        self.assertIn("merge_test_1_EXIT=79", output)
        self.assertNotIn("RERUN", output)

    def test_queue_loss_after_guest_exit_80_preserves_checkpoint_settlement(self) -> None:
        fixture = GatePlacementFixture(self)
        write_gate(fixture.step.folder, COMMAND, GATE_UNSETTLED_LAUNCH_STATUS, "guest end unproven\n")

        @contextmanager
        def tool_folder(_tool: str) -> Generator[Path]:
            try:
                yield fixture.step.folder
            finally:
                raise OSError("fixture folder cleanup failed")

        with (PlacementFixture(),
              mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": ""}, clear=False),
              mock.patch.object(first_step, "gate_targets", return_value=("mac-linux",)),
              mock.patch.object(first_step, "_gate_request", return_value=fixture.request),
              mock.patch.object(first_step, "_placement_grant", return_value=admission_wire.Assignment(fixture.guest)),
              mock.patch.object(admission_records, "process_start_time", return_value="456"),
              mock.patch.object(build_folders, "tool_folder", side_effect=tool_folder),
              mock.patch.object(admission_client, "_connect", side_effect=FileNotFoundError("fixture socket is gone")) as connect,
              mock.patch.object(checkpoint_gate, "run_test_command", side_effect=fixture.run_gate_step) as runner):
            del os.environ["BUILDLOG_PLACEMENT_FILE"]
            with self.assertRaises(checkpoint_gate.Stop) as stopped:
                _ = checkpoint_gate.run_test_steps(fixture.production, [fixture.step], frozenset(),
                                                   frozenset(), fixture.log)
        self.assertEqual(stopped.exception.state, "blocked")
        self.assertIn("launch settlement", stopped.exception.detail)
        connect.assert_called_once()
        runner.assert_called_once_with(COMMAND, fixture.step.folder, merge_head="")
        output = fixture.log.read_text()
        self.assertIn("guest end unproven", output)
        self.assertIn("Gate cleanup failed. no socket", output)
        self.assertIn("Gate cleanup failed. fixture folder cleanup failed", output)
        self.assertIn("merge_test_1_EXIT=80", output)
        self.assertNotIn("RERUN", output)

    def test_unsettled_attempt_blocks_another_launch(self) -> None:
        fixture = GatePlacementFixture(self)
        fixture.report_failure.return_value = admission_wire.DispatchStatus(fixture.guest, placement.CurrentAssignment(),
                                                                        placement.PermissionAvailable())
        run = mock.Mock(return_value=fixture.refuse_guest())
        with self.assertRaises(checkpoint_gate.Stop) as stopped:
            _ = fixture.run_step(run, retry_failed_steps=True)
        self.assertEqual(stopped.exception.state, "blocked")
        fixture.place_again.assert_not_called()
        run.assert_called_once()

    def test_red_gate_ignores_a_stale_refusal_file(self) -> None:
        fixture = GatePlacementFixture(self)
        _ = fixture.refuse_guest()
        run = mock.Mock(return_value=subprocess.CompletedProcess(COMMAND, 1, RED_OUTPUT, ""))
        with self.assertRaises(checkpoint_gate.RedCheckpointTests) as stopped:
            _ = fixture.run_step(run)
        self.assertEqual(stopped.exception.red[0].names, frozenset({"lib/test_infrastructure.py"}))
        fixture.report_failure.assert_not_called()
        fixture.place_again.assert_not_called()

    def test_red_gate_remains_a_test_failure(self) -> None:
        self.check_exit_status(1, retry_failed_steps=False)
        self.check_exit_status(0, retry_failed_steps=False,
                               output="--- lib/test_infrastructure.py passed on rerun alone; first run exit=1 ---\n")

    def test_copy_setup_errors_block_the_checkpoint_without_a_test_retry(self) -> None:
        for status in (copies.COPY_ERROR_STATUS, os.EX_TEMPFAIL, 127):
            with self.subTest(status=status):
                self.check_exit_status(status, retry_failed_steps=True)

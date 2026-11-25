"""Check failed probes, repository declines and recovery with private targets."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import threading
from typing import cast, override
import unittest
from unittest import mock

from scripts.lib.helper_tree_test_support import COPY_HELPERS, copy_helpers, verify_test_environment

from scripts.build_hold.owning_session import NoOwningSession
from scripts.aws_target import ledger
from scripts.build_queue import admission_protocol as wire, admission_service as service
from scripts.build_queue.admission_test_support import caller_admission
from scripts.build_targets import build_targets, target_config, targets
from scripts.direct.verify_lint_scope_test_support import VerifyLintScopeFixture
from scripts.lib.process_group import send_process_group
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE, write_config_fixture
from scripts.mac_test import eligibility, mac_test, offload, claim_state
from scripts.mac_test.offload_test_support import OffloadCommandFixture

VERIFY = Path(__file__).resolve().parents[1] / "direct/verify.sh"


class FailedProbeTests(OffloadCommandFixture):
    linux_settings: Path = Path()

    @override
    def setUp(self) -> None:
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, verify_test_environment(), clear=True)))
        super().setUp()
        self.linux_settings = self.root / "box.conf"
        _ = write_config_fixture(self.linux_settings,
            "offload=on\nhost=fake-box\nkinds=test,check,lint,doc,build\n"
            + "shell_timeout_s=2\nparity.rustc=fixture\nparity.nextest=fixture\n"
            + "parity.mend=fixture\nparity.scripts_commit=fixture\n", template="mac_test")
        _ = (self.root / "build_targets.conf").write_text(
            "targets=mac,natedev,box\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\nkind.box=ssh\nplatform.box=linux\nsettings.box=box.conf\n"
            + UNREACHABLE_WAIT_FIXTURE)
        self.environment.update({name: "" for name in (
            "UNIT_RUN_DIR", "UNIT_SESSION_DIR", "UNIT_TEAM_ROLE", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID")})
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))

    @property
    def identity(self) -> eligibility.KnownRepository:
        identity = eligibility.repository_identity(self.repository)
        if not isinstance(identity, eligibility.KnownRepository):
            self.fail(f"The fixture repository has no identity: {self.repository}")
        return identity

    def probe(self, target: str = "box") -> offload.ProbeResult:
        self.sync_fixture()
        config = eligibility.read_config(self.config_path if target == "mac" else self.linux_settings)
        return offload.probe_mac(offload.ProcessController(), config, self.identity, target)

    def healthy(self) -> None:
        # Recovery starts after the configured failure backoff expires.
        for target in ("mac", "box"):
            offload.write_backoff(time.time() - 1, target)
        self.fixture["probe_status"] = 0
        self.fixture["probe_lines"] = ["free_gib=120", "host=fixture", "rc=0",
                                       "nextest=fixture", "mend=fixture", "scripts_commit=fixture"]
        self.sync_fixture()

    def free(self, target: str = "box", repository: Path | None = None) -> str:
        args = mac_test.CliArguments()
        args.target = target
        args.repo_root = str(self.repository if repository is None else repository)
        args.settings = str(self.config_path if target == "mac" else self.linux_settings)
        output = io.StringIO()
        with redirect_stdout(output):
            status = mac_test.command_free_now(args, mac_test.state_paths(target))
        answer = output.getvalue().strip()
        self.assertEqual(status, 0 if answer == "free" else 1)
        return answer

    def reserve(self, *, new_step: bool = False) -> int:
        args = build_targets.CliArguments()
        args.kind, args.repo_root, args.package = "test", str(self.repository), "hana"
        args.target, args.pid = "box", os.getpid()
        args.new_step = new_step
        args.reservation_file = str(self.root / "reservation")
        return build_targets.command_reserve(args)

    def release_reservation(self) -> int:
        claim, = mac_test.read_claims(mac_test.state_paths("box"))
        return build_targets.command_release_wait(os.getpid(),
            identity=claim_state.claim_identity(claim.record), session=NoOwningSession())

    def test_probe_resolves_target_before_taking_its_start_stamp(self) -> None:
        """offload.probe_mac row 1."""
        calls: list[str] = []
        resolve = eligibility.configured_ssh_target

        def target(name: str) -> target_config.SshTarget:
            calls.append("target")
            return resolve(name)

        def started() -> int:
            calls.append("started")
            return 100

        def capture(_arguments: list[str], _deadline: offload.CommandDeadline) -> offload.CommandOutput:
            calls.append("ssh")
            return offload.CommandOutput(0, "rc=0\nfree_gib=42\nhost=fixture\n", "")

        config = eligibility.read_config(self.config_path)
        with (mock.patch.object(eligibility, "configured_ssh_target", side_effect=target),
              mock.patch.object(time, "time_ns", side_effect=started),
              mock.patch.object(offload.ProcessController, "capture", side_effect=capture),
              mock.patch.object(offload, "record_reachable", wraps=offload.record_reachable) as reachable):
            self.assertEqual(offload.probe_mac(offload.ProcessController(), config, self.identity),
                             offload.ProbeSnapshot(42, "fixture"))
        self.assertEqual(calls, ["target", "started", "target", "target", "ssh"])
        reachable.assert_called_once_with("mac", probe_started_ns=100)

    def test_linux_probe_requires_execution_settings_before_ssh_and_fence(self) -> None:
        """offload.probe_mac row 2."""
        from scripts.lib.atomic_file import write_text_atomic
        from scripts.lib import conf

        settings = eligibility.probe_settings(conf.read(self.linux_settings))
        controller = offload.ProcessController()
        controller.claim_identity = claim_state.ClaimIdentity("claim", "episode", "attempt")
        paths = mac_test.state_paths("box")
        with (mock.patch.object(controller, "capture") as ssh,
              mock.patch.object(offload, "write_text_atomic", wraps=write_text_atomic) as write,
              self.assertRaisesRegex(ValueError, "requires execution settings")):
            _ = offload.probe_mac(controller, settings, self.identity, "box")
        ssh.assert_not_called()
        write.assert_not_called()
        self.assertFalse((paths.directory / "pending-starts").exists())

    def test_mac_probe_resolves_mirror_before_writing_start_fence(self) -> None:
        """offload.probe_mac row 5."""
        from scripts.lib.atomic_file import write_text_atomic
        from scripts.copies import copies

        controller = offload.ProcessController()
        controller.claim_identity = claim_state.ClaimIdentity("claim", "episode", "attempt")
        config = eligibility.read_config(self.config_path)
        calls = mock.Mock()
        with (mock.patch.object(offload, "mac_mirror_root", wraps=offload.mac_mirror_root) as mirror,
              mock.patch.object(copies, "mirror_probe_setup", wraps=copies.mirror_probe_setup) as setup,
              mock.patch.object(offload, "write_text_atomic", wraps=write_text_atomic) as write,
              mock.patch.object(controller, "capture",
                                return_value=offload.CommandOutput(0, "rc=0\nfree_gib=42\nhost=fixture\n", "")) as ssh):
            for name, spy in (("mirror", mirror), ("setup", setup), ("write", write), ("ssh", ssh)):
                calls.attach_mock(spy, name)
            result = offload.probe_mac(controller, config, self.identity)
        self.assertEqual(result, offload.ProbeSnapshot(42, "fixture"))
        self.assertEqual([call[0] for call in calls.mock_calls], ["mirror", "setup", "write", "ssh"])

    def test_probe_uses_bounded_ssh_before_claim_state_lock(self) -> None:
        """offload.probe_mac row 7."""
        controller = offload.ProcessController()
        controller.claim_identity = claim_state.ClaimIdentity("claim", "episode", "attempt")
        config = eligibility.read_config(self.config_path)
        lock_path = mac_test.state_paths().lock

        def capture(arguments: list[str], deadline: offload.CommandDeadline) -> offload.CommandOutput:
            self.assertEqual(arguments[0], "ssh")
            self.assertEqual(arguments[-2], config.host)
            self.assertEqual(deadline, offload.CommandTimeLimit(config.probe_timeout_s))
            self.assertFalse(lock_path.exists())
            return offload.CommandOutput(0, "rc=0\nfree_gib=123\nhost=fixture-host\n", "")

        with mock.patch.object(controller, "capture", side_effect=capture) as ssh:
            result = offload.probe_mac(controller, config, self.identity)
        ssh.assert_called_once()
        self.assertEqual(result, offload.ProbeSnapshot(123, "fixture-host"))

    def test_unresolved_linux_startup_does_not_publish_probe_failure(self) -> None:
        """offload.probe_mac row 8."""
        controller = offload.ProcessController()
        config = eligibility.read_config(self.linux_settings)
        with (mock.patch.object(controller, "capture", side_effect=[
                  offload.CommandOutput(0, "", ""),
                  offload.CommandOutput(1, "linux_startup=unresolved\n", "")]) as ssh,
              mock.patch.object(claim_state, "locked", wraps=claim_state.locked) as lock):
            result = offload.probe_mac(controller, config, self.identity, "box")
        self.assertEqual(result, offload.UnresolvedSlotStartup())
        self.assertEqual(ssh.call_count, 2)
        lock.assert_not_called()
        self.assertFalse((mac_test.state_paths("box").directory / "failed_probe").exists())

    def test_probe_failure_resolves_mark_path_after_ssh_before_lock(self) -> None:
        """Probe result and backoff helpers row 5."""
        controller = offload.ProcessController()
        config = eligibility.read_config(self.config_path)
        lock_path = mac_test.state_paths().directory / offload.PROBE_RESULT_LOCK_NAME
        calls = mock.Mock()
        with (mock.patch.object(eligibility, "state_directory", wraps=eligibility.state_directory) as directory,
              mock.patch.object(claim_state, "locked", wraps=claim_state.locked) as lock,
              mock.patch.object(controller, "capture", return_value=offload.COMMAND_TIMED_OUT) as ssh):
            for name, spy in (("directory", directory), ("lock", lock), ("ssh", ssh)):
                calls.attach_mock(spy, name)
            result = offload.probe_mac(controller, config, self.identity)
        self.assertEqual(result, offload.UNAVAILABLE_PROBE)
        self.assertEqual([call[0] for call in calls.mock_calls], ["ssh", "directory", "lock"])
        lock.assert_called_once_with(lock_path, wait=False)
        self.assertEqual((lock_path.parent / "failed_probe").read_text(), "failed\n")

    def test_failed_probe_does_not_wait_for_the_result_lock(self) -> None:
        """Probe result and backoff helpers row 6."""
        controller = offload.ProcessController()
        config = eligibility.read_config(self.config_path)
        lock_path = mac_test.state_paths().directory / offload.PROBE_RESULT_LOCK_NAME
        with (claim_state.locked(lock_path),
              mock.patch.object(controller, "capture", return_value=offload.COMMAND_TIMED_OUT),
              mock.patch.object(claim_state, "locked", wraps=claim_state.locked) as lock):
            result = offload.probe_mac(controller, config, self.identity)
        self.assertEqual(result, offload.UNAVAILABLE_PROBE)
        lock.assert_called_once_with(lock_path, wait=False)
        self.assertFalse((lock_path.parent / "failed_probe").exists())

    def test_backoff_write_obeys_the_caller_lock_mode(self) -> None:
        """Probe result and backoff helpers row 9."""
        path = eligibility.backoff_path("box")
        lock_path = path.parent / offload.PROBE_RESULT_LOCK_NAME
        deadline = time.time() + 60
        with (claim_state.locked(lock_path),
              mock.patch.object(claim_state, "locked", wraps=claim_state.locked) as lock):
            offload.write_backoff(deadline, "box", wait=False)
            lock.assert_called_once_with(lock_path, wait=False)
            self.assertFalse(path.exists())
        with mock.patch.object(claim_state, "locked", wraps=claim_state.locked) as lock:
            offload.write_backoff(deadline, "box")
        lock.assert_called_once_with(lock_path, wait=True)
        self.assertEqual(path.read_text(), f"{deadline:.6f}\n")

    def test_mac_probe_locks_claim_state_after_transport_for_watch_origin(self) -> None:
        """offload.probe_mac row 10."""
        from scripts.mac_test.mirror_root import MacMirrorRoot

        config = eligibility.read_config(self.config_path)
        controller = offload.ProcessController()
        controller.claim_target = "mac"
        identity = claim_state.ClaimIdentity("claim", "episode", "attempt")
        controller.claim_identity = identity
        repository = claim_state.MacRepositoryIdentity("key", "fixture", "fixture.json", "fixture",
                                                       claim_state.KnownMacHistoryKey("fixture-history"))
        controller.mac_repository = repository
        paths = claim_state.state_paths("mac")
        path = paths.claims / repository.claim_file
        path.parent.mkdir(parents=True, exist_ok=True)
        machine = "f0000000-0000-0000-0000-000000000000"
        root = MacMirrorRoot(".local/state/fixture-mirror")
        record: claim_state.RemoteJobClaim = {
            "claim_id": identity.claim_id, "episode": identity.episode, "attempt": identity.attempt,
            "submitting_session": "", "pid": os.getpid(), "proc_start": "fixture-start", "what": "fixture",
            "worktree": str(self.repository), "since": "2026-11-01T12:00:00+00:00", "remote_host": config.host,
            "repo_key": repository.key, "repo_name": repository.mirror_name,
            "mac_repository": repository.record(),
        }
        mac_test.write_json(path, record)
        response = f"rc=0\nfree_gib=42\nhost=fixture\ndispatch_mac_id={machine}\n"
        token = offload.offload_mirror_root.set(root)
        self.addCleanup(offload.offload_mirror_root.reset, token)
        trace = mock.Mock()
        with (mock.patch.object(controller, "capture",
                                return_value=offload.CommandOutput(0, response, "")) as ssh,
              mock.patch.object(claim_state, "locked", wraps=claim_state.locked) as lock):
            trace.attach_mock(ssh, "ssh")
            trace.attach_mock(lock, "lock")
            result = offload.probe_mac(controller, config, self.identity)
        self.assertEqual(result, offload.ProbeSnapshot(42, "fixture"))
        self.assertEqual([call[0] for call in trace.mock_calls], ["ssh", "lock", "lock"])
        self.assertEqual(lock.call_args_list[0], mock.call(paths.lock))

    def test_mac_probe_rereads_changed_claim_before_watch_publication(self) -> None:
        """offload.probe_mac row 11."""
        from scripts.mac_test.mirror_root import MacMirrorRoot

        config = eligibility.read_config(self.config_path)
        controller = offload.ProcessController()
        controller.claim_target = "mac"
        identity = claim_state.ClaimIdentity("claim", "episode", "attempt")
        controller.claim_identity = identity
        repository = claim_state.MacRepositoryIdentity("key", "fixture", "fixture.json", "fixture",
                                                       claim_state.KnownMacHistoryKey("fixture-history"))
        controller.mac_repository = repository
        paths = claim_state.state_paths("mac")
        path = paths.claims / repository.claim_file
        path.parent.mkdir(parents=True, exist_ok=True)
        machine = "f0000000-0000-0000-0000-000000000000"
        root = MacMirrorRoot(".local/state/fixture-mirror")
        record: claim_state.RemoteJobClaim = {
            "claim_id": identity.claim_id, "episode": identity.episode, "attempt": identity.attempt,
            "submitting_session": "", "pid": os.getpid(), "proc_start": "fixture-start", "what": "fixture",
            "worktree": str(self.repository), "since": "2026-11-01T12:00:00+00:00", "remote_host": config.host,
            "repo_key": repository.key, "repo_name": repository.mirror_name,
            "mac_repository": repository.record(),
        }
        mac_test.write_json(path, record)
        response = f"rc=0\nfree_gib=42\nhost=fixture\ndispatch_mac_id={machine}\n"
        token = offload.offload_mirror_root.set(root)
        self.addCleanup(offload.offload_mirror_root.reset, token)
        def capture(_command: list[str], _deadline: offload.CommandDeadline) -> offload.CommandOutput:
            record["remote_host"] = "replacement-host"
            mac_test.write_json(path, record)
            return offload.CommandOutput(0, response, "")

        with (mock.patch.object(controller, "capture", side_effect=capture),
              mock.patch.object(claim_state, "read_run", wraps=claim_state.read_run) as read):
            result = offload.probe_mac(controller, config, self.identity)
        self.assertEqual(result, offload.ProbeSnapshot(42, "fixture"))
        read.assert_called_once_with(path)
        self.assertEqual(claim_state.read_run(path), record)

    def test_mac_probe_saves_and_clears_the_exact_claims_watch_origin(self) -> None:
        """offload.probe_mac row 12."""
        from scripts.mac_test.mirror_root import MacMirrorRoot

        config = eligibility.read_config(self.config_path)
        controller = offload.ProcessController()
        controller.claim_target = "mac"
        identity = claim_state.ClaimIdentity("claim", "episode", "attempt")
        controller.claim_identity = identity
        repository = claim_state.MacRepositoryIdentity("key", "fixture", "fixture.json", "fixture",
                                                       claim_state.KnownMacHistoryKey("fixture-history"))
        controller.mac_repository = repository
        paths = claim_state.state_paths("mac")
        path = paths.claims / repository.claim_file
        path.parent.mkdir(parents=True, exist_ok=True)
        machine = "f0000000-0000-0000-0000-000000000000"
        root = MacMirrorRoot(".local/state/fixture-mirror")
        record: claim_state.RemoteJobClaim = {
            "claim_id": identity.claim_id, "episode": identity.episode, "attempt": identity.attempt,
            "submitting_session": "", "pid": os.getpid(), "proc_start": "fixture-start", "what": "fixture",
            "worktree": str(self.repository), "since": "2026-11-01T12:00:00+00:00", "remote_host": config.host,
            "repo_key": repository.key, "repo_name": repository.mirror_name,
            "mac_repository": repository.record(),
        }
        mac_test.write_json(path, record)
        response = f"rc=0\nfree_gib=42\nhost=fixture\ndispatch_mac_id={machine}\n"
        token = offload.offload_mirror_root.set(root)
        self.addCleanup(offload.offload_mirror_root.reset, token)
        with mock.patch.object(controller, "capture", side_effect=[
                offload.CommandOutput(0, response, ""),
                offload.CommandOutput(0, "rc=0\nfree_gib=42\nhost=fixture\n", "")]) as ssh:
            self.assertEqual(offload.probe_mac(controller, config, self.identity),
                             offload.ProbeSnapshot(42, "fixture"))
            saved = claim_state.read_run(path)
            assert not isinstance(saved, claim_state.NoRun)
            self.assertEqual(saved.get("mac_watch_origin"), {"mirror_root": root.relative, "machine_id": machine})
            self.assertEqual(offload.probe_mac(controller, config, self.identity),
                             offload.ProbeSnapshot(42, "fixture"))
        self.assertEqual(ssh.call_count, 2)
        self.assertEqual(claim_state.read_run(path), record)

    def test_interrupted_fresh_claim_releases_the_exact_claim(self) -> None:
        """Probe recovery start and worker row 11."""
        from collections.abc import Sequence

        config = eligibility.read_config(self.config_path)
        controller = offload.ProcessController()
        request = offload.OffloadRequest(self.repository, "hana", "fixture", offload.NoResultFile(), (),
                                         owner="fixture", holder_session="fixture-session")
        identity = claim_state.ClaimIdentity("fixture", "episode", "attempt")

        def claim(_arguments: Sequence[str], attempt: offload.LocalClaimAttempt) -> None:
            controller.claim_identity = identity
            attempt.answer = offload.FreshMacClaim()
            attempt.interruption = offload.OffloadInterrupted(signal.SIGTERM)

        def release(current: offload.ProcessController, target: str) -> int:
            self.assertEqual(current.claim_identity, identity)
            self.assertEqual(target, request.target)
            return 0

        with (mock.patch.object(controller, "complete_claim", side_effect=claim),
              mock.patch.object(offload, "release_claim", side_effect=release) as released,
              self.assertRaises(offload.OffloadInterrupted) as interrupted):
            _ = offload.claim_for_run(controller, config, request, self.identity)
        self.assertEqual(interrupted.exception.signal_number, signal.SIGTERM)
        released.assert_called_once_with(controller, request.target)

    def test_recovery_rejects_missing_saved_mirror_before_copying(self) -> None:
        """Probe recovery start and worker row 12."""
        from contextlib import nullcontext
        from scripts.copies import copies

        args = build_targets.CliArguments()
        args.target = "mac"
        args.kind = "check"
        args.package = "hana"
        args.repo_root = str(self.repository)
        args.recovery_fd = os.open(os.devnull, os.O_RDONLY)
        with (mock.patch.object(mac_test, "free_now", return_value="bad"),
              mock.patch.object(offload, "interrupt_handlers", return_value=nullcontext()),
              mock.patch.object(copies, "remote_lease", return_value=nullcontext()),
              mock.patch.object(offload, "claim_for_run", return_value=offload.FreshMacClaim()) as claim,
              mock.patch.object(offload, "copy_tracked_scripts", wraps=offload.copy_tracked_scripts) as copy,
              mock.patch.object(offload, "release_claim") as release):
            result = build_targets.command_recover_probe(args)
        self.assertEqual(result, 0)
        claim.assert_called_once()
        copy.assert_not_called()
        release.assert_called_once()

    def test_script_copy_rechecks_cleanliness_and_head_before_remote_stamp(self) -> None:
        """Probe recovery start and worker row 15."""
        from collections.abc import Sequence

        controller = offload.ProcessController()
        config = eligibility.read_config(self.linux_settings)
        commit = "a" * 40
        commands: list[str] = []
        replies = iter((
            offload.CommandOutput(0, "scripts/mac_test/linux_runner.py\0", ""),
            offload.CommandOutput(0, "", ""),
            offload.CommandOutput(0, commit + "\n", ""),
            offload.CommandOutput(0, "", ""),
            offload.CommandOutput(1, "", ""),
            offload.CommandOutput(0, "b" * 40 + "\n", ""),
        ))

        def capture(arguments: Sequence[str], *_options: object) -> offload.CommandOutput:
            commands.append(arguments[3] if arguments[0] == "git" else arguments[0])
            return next(replies)

        with (mock.patch.object(controller, "capture_copy", side_effect=capture),
              mock.patch.object(offload, "expected_linux_parity", return_value={"scripts_commit": commit})):
            self.assertFalse(offload.copy_tracked_scripts(controller, config))
        self.assertEqual(commands, ["ls-files", "diff", "rev-parse", "rsync", "diff", "rev-parse"])

    def test_two_mac_eligibility_reads_share_the_failure_backoff(self) -> None:
        target = target_config.configured_ssh_target("mac")
        for failure in (offload.COMMAND_COULD_NOT_START, offload.COMMAND_TIMED_OUT,
                        offload.CommandOutput(255, "", "private connection refused")):
            with self.subTest(failure=type(failure).__name__):
                eligibility.backoff_path("mac").unlink(missing_ok=True)
                (eligibility.state_directory("mac") / "failed_probe").unlink(missing_ok=True)
                with mock.patch.object(offload.ProcessController, "capture", return_value=failure) as capture, \
                     redirect_stderr(io.StringIO()):
                    first = build_targets.mac_target_eligibility(target, self.repository, "hana", "test",
                        targets.ANY_ELIGIBLE_TARGET, session=NoOwningSession())
                    second = build_targets.mac_target_eligibility(target, self.repository, "hana", "test",
                        targets.ANY_ELIGIBLE_TARGET, session=NoOwningSession())
                self.assertIsInstance(first, targets.ExcludedTarget)
                self.assertIsInstance(second, targets.ExcludedTarget)
                self.assertEqual(capture.call_count, 1)
                self.assertIsInstance(eligibility.read_backoff(time.time(), "mac"), eligibility.ActiveBackoff)
                self.assertIsInstance(eligibility.read_failed_probe("mac"), eligibility.FailedProbe)

    def test_probe_logs_only_failure_and_recovery_transitions(self) -> None:
        config = eligibility.read_config(self.config_path)
        errors = io.StringIO()
        with mock.patch.object(offload.ProcessController, "capture", return_value=offload.COMMAND_COULD_NOT_START) as capture, \
             redirect_stderr(errors):
            for _ in range(3):
                self.assertIsInstance(offload.probe_mac(offload.ProcessController(), config, self.identity),
                                      offload.UnavailableProbe)
            self.assertEqual(capture.call_count, 3)
            self.assertEqual(len(errors.getvalue().splitlines()), 1)
            capture.return_value = offload.CommandOutput(0, "free_gib=120\nhost=fixture\nrc=0\n", "")
            self.assertIsInstance(offload.probe_mac(offload.ProcessController(), config, self.identity),
                                  offload.ProbeSnapshot)
        lines = errors.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("could not start", lines[0])
        self.assertIn("reachable again", lines[1])
        self.assertIsInstance(eligibility.read_failed_probe("mac"), eligibility.NoFailedProbe)
        self.assertIsInstance(eligibility.read_backoff(time.time(), "mac"), eligibility.NoBackoff)

    def test_version_decline_stays_repository_specific_after_backoff_expires(self) -> None:
        self.healthy()
        self.fixture["probe_lines"][-3] = "nextest=wrong"
        with redirect_stderr(io.StringIO()):
            probe = self.probe()
        self.assertIsInstance(probe, offload.UnavailableProbe)
        self.assertIsInstance(eligibility.read_backoff(time.time(), "box"), eligibility.NoBackoff)
        self.assertEqual(self.free(), "bad")
        offload.write_backoff(time.time() - 1, "box")
        _ = eligibility.linux_version_decline_path("box", "hana").write_text("0")
        self.assertEqual(self.free(), "bad")
        other = self.root / "other"
        _ = subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True)
        self.assertEqual(self.free(repository=other), "free")
        self.healthy()
        self.assertIsInstance(self.probe(), offload.ProbeSnapshot)
        self.assertEqual(self.free(), "bad", "A target probe must keep a repository decline")

    def test_failed_reach_stays_bad_for_every_repository_until_a_good_probe(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        offload.write_backoff(time.time() - 1, "box")
        other = self.root / "other"
        _ = subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True)
        self.assertEqual(self.free(), "bad")
        self.assertEqual(self.free(repository=other), "bad")
        self.healthy()
        self.assertIsInstance(self.probe(), offload.ProbeSnapshot)
        self.assertEqual(self.free(), "free")
        self.assertEqual(self.free(repository=other), "free")

    def test_waiting_reservation_refuses_bad_but_new_step_reaches_the_claim(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        before = len(self.events())
        self.assertEqual(self.reserve(), 1)
        self.assertEqual(len(self.events()), before)
        self.assertEqual(self.reserve(new_step=True), 0)
        claim = next(claim for claim in mac_test.read_claims(mac_test.state_paths("box"))
                     if claim.record["pid"] == os.getpid())
        self.assertEqual(claim.record["what"], "nextest hana")
        self.assertEqual((self.root / "reservation").read_text(), f"box\t{self.linux_settings}\n")
        self.assertGreater(len(self.events()), before)
        self.assertEqual(self.release_reservation(), 0)

    def run_reserve_cli(self, *, new_step: bool) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(Path(build_targets.__file__)), "reserve", "--target", "box",
                   "--repo-root", str(self.repository), "--package", "hana", "--kind", "test",
                   "--pid", str(os.getpid()), "--reservation-file", str(self.root / "ssh-reservation")]
        if new_step:
            command.append("--new-step")
        return subprocess.run(command, capture_output=True, text=True, check=False, env=self.environment)

    def test_retry_refuses_a_failed_target_without_a_new_probe(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        before = self.events_for("ssh")
        result = self.run_reserve_cli(new_step=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.events_for("ssh"), before)
        self.assertEqual(self.free(), "bad")
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("box")), ())

    def test_new_step_reserve_command_reaches_a_bad_target(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        before = self.events_for("ssh")
        result = self.run_reserve_cli(new_step=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        probes = self.events_for("ssh")[len(before):]
        self.assertEqual(len(probes), 1, probes)
        self.assertEqual(probes[0]["args"][-1], "true")
        self.assertFalse(probes[0]["claimed"], "the probe must precede the claim")
        self.assertEqual((self.root / "ssh-reservation").read_text(), f"box\t{self.linux_settings}\n")
        self.assertEqual(self.release_reservation(), 0)

    def test_bad_mark_preserves_busy_blocked_and_off_precedence(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        self.assertEqual(self.reserve(new_step=True), 0)
        self.assertEqual(self.free(), "busy")
        self.assertEqual(self.release_reservation(), 0)
        paths = mac_test.state_paths("box")
        _ = paths.block.write_text(json.dumps({"version": 2, "holder": "fixture", "for": "test",
            "since": "2026-01-01T00:00:00+00:00", "expires": "2099-01-01T00:00:00+00:00",
            "session": None, "showrunner": None,
            "state": "active", "ci": "not_configured", "free_message": {"kind": "not_needed"}}))
        self.assertEqual(self.free(), "blocked")
        _ = write_config_fixture(self.linux_settings, "offload=off\n", template="mac_test")
        self.assertEqual(self.free(), "off")

    def test_reach_error_prints_only_the_last_nonempty_stderr_line(self) -> None:
        self.fixture["probe_status"] = 1
        self.fixture["probe_error"] = "Traceback (most recent call last):\n  broken call\nValueError: fixture failed\n\n"
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertIsInstance(self.probe("mac"), offload.UnavailableProbe)
        self.assertEqual(output.getvalue().splitlines(),
                         ["mac_test: could not reach the Mac: ValueError: fixture failed"])
        offload.write_backoff(time.time() - 1, "mac")
        self.assertEqual(self.free("mac"), "bad")


    def test_failed_mark_precedes_aws_status_and_mac_status_then_clears(self) -> None:
        self.fixture["probe_status"] = 255
        for name in ("mac", "box"):
            with redirect_stderr(io.StringIO()):
                self.assertIsInstance(self.probe(name), offload.UnavailableProbe)
        linux = target_config.SshTarget("box", "linux", self.root / "linux.conf")
        self.assertIsInstance(targets.target_state(linux), targets.Unreachable)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(mac_test.command_target_status(mac_test.state_paths("mac")), 0)
        self.assertEqual(output.getvalue(), "unreachable: failed probe\n")
        self.healthy()
        for name in ("mac", "box"):
            self.assertIsInstance(self.probe(name), offload.ProbeSnapshot)
        self.assertIsInstance(targets.target_state(target_config.configured_ssh_target("box")), targets.Free)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(mac_test.command_target_status(mac_test.state_paths("mac")), 0)
        self.assertEqual(output.getvalue(), "free\n")

    def install_script_copy_tools(self) -> None:
        stub = self.bin_directory / "command-stub"
        source = stub.read_text().replace('command=${0##*/}',
            'command=${0##*/}\n'
            + 'if [[ $command == rsync ]]; then printf "start %s\\n" "$EPOCHREALTIME" >> "$root/probe-timing"; fi')
        source = source.replace("printf 'started\\n' > \"$root/probe-started\"",
            'if [[ ${!#} == *".build-target-script-commit"* && ${!#} == printf* ]]; then\n'
            + '    printf fixture > "$root/remote-stamp"\nfi\n'
            + "printf 'started\\n' > \"$root/probe-started\"")
        source = source.replace("for line in \"${probe_lines[@]}\"; do printf '%s\\n' \"$line\"; done",
            'for line in "${probe_lines[@]}"; do\n'
            + '    if [[ $line == scripts_commit=fixture ]]; then line="scripts_commit=$(cat "$root/remote-stamp")"; fi\n'
            + "    printf '%s\\n' \"$line\"\ndone")
        receiver = self.stub_directory / "receive-tree.py"
        self.write_executable(receiver,
            'import shlex, subprocess, sys\nfrom pathlib import Path\n'
            + 'from scripts.copies import copies\n'
            + 'source = Path(sys.argv[1])\n'
            + 'destination = Path.home() / shlex.split(sys.argv[2].split(":", 1)[1])[0]\n'
            + 'destination.mkdir(parents=True, exist_ok=True)\n'
            # The real copy excludes /.git, and the fixture's Git objects are read-only.
            + 'for entry in source.iterdir():\n'
            + '    if entry.name != ".git":\n'
            + '        subprocess.run(["cp", "-a", str(entry), str(destination)], check=True)\n'
            + 'copies.mark_mirror(destination, "verify", "hana", "fixture")\n')
        source = source.replace('    exit "$rsync_status"',
            '    if [[ " $* " != *" --files-from="* ]]; then\n'
            + f'        {shlex.quote(sys.executable)} {shlex.quote(str(receiver))} "${{@: -2:1}}" "${{!#}}" || exit $?\n'
            + '    fi\n    exit "$rsync_status"')
        os.environ["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "lib/python_startup")
        self.environment["PYTHONPATH"] = os.environ["PYTHONPATH"]
        source = source.replace('exit "$probe_status"',
            'printf "end %s\\n" "$EPOCHREALTIME" >> "$root/probe-timing"\nexit "$probe_status"')
        _ = stub.write_text(source)
        real_git = subprocess.run(["bash", "-c", "command -v git"], capture_output=True,
                                  text=True, check=True).stdout.strip()
        self.write_executable(self.bin_directory / "git",
            'import os, sys\nfrom pathlib import Path\nargs=sys.argv[1:]\n'
            + 'if "ls-files" in args and "scripts" in args and "config" in args:\n print("scripts/mac_test/linux_runner.py", end="\\0")\n'
            + 'elif "rev-parse" in args and args[-1] == "HEAD":\n print("fixture")\n'
            + 'elif "diff" in args:\n pass\n'
            + f'else:\n os.execv({real_git!r}, [{real_git!r}, *args])\n')

    def test_new_step_copies_the_tree_and_deduplicates_the_probe(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        offload.write_version_decline("box", "hana")
        decline = eligibility.linux_version_decline_path("box", "hana").read_text()
        self.healthy()
        self.fixture["hold_probe"] = True
        self.sync_fixture()
        self.install_script_copy_tools()
        _ = (self.stub_directory / "remote-stamp").write_text("old")
        mirror = Path(self.environment["HOME"]) / offload.remote_mirror(self.identity, "box")
        mirror.mkdir(parents=True)
        stale_source = mirror / "stale-source"
        _ = stale_source.write_text("old repository tree")
        _ = (self.repository / "stale-source").write_text("current repository tree")
        target = target_config.configured_ssh_target("box")
        started = time.monotonic()
        result = build_targets.ssh_target_eligibility(target, self.repository, "hana", "test",
                                                     targets.LocalRequired("local step"), new_step=True)
        self.assertIsInstance(result, targets.ExcludedTarget)
        # The worker holds the probe gate. The eligibility answer must return first.
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = build_targets.ssh_target_eligibility(target, self.repository, "hana", "test",
                                                     targets.ANY_ELIGIBLE_TARGET, new_step=True)
        self.assertIsInstance(result, targets.EligibleTarget)
        descriptor = os.open(self.stub_directory / "continue-probe", os.O_WRONLY)
        try:
            _ = os.write(descriptor, b"continue\ncontinue\ncontinue\n")
        finally:
            os.close(descriptor)
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        elapsed = time.monotonic() - started
        self.assertIsInstance(eligibility.read_failed_probe("box"), eligibility.NoFailedProbe)
        self.assertEqual(eligibility.linux_version_decline_path("box", "hana").read_text(), decline)
        self.assertEqual(stale_source.read_text(), "current repository tree")
        copies = [event for event in self.events() if event["command"] == "rsync"]
        self.assertEqual(len(copies), 2)
        self.assertTrue(any(word.startswith("--files-from=") for word in copies[0]["args"]))
        self.assertIn("--delete", copies[1]["args"])
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("box")), ())
        timing = [line.split() for line in (self.stub_directory / "probe-timing").read_text().splitlines()]
        copy_started = next(float(words[1]) for words in timing if words[0] == "start")
        probe_ended = float(timing[-1][1])
        print(f"RECOVERY script copy plus probe: {probe_ended - copy_started:.6f} s; worker total: {elapsed:.6f} s")

    def test_new_pinned_step_reserves_before_recovery_can_take_its_claim(self) -> None:
        self.healthy()
        self.fixture["probe_lines"][-3] = "nextest=wrong"
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.assertEqual(self.free(), "bad")
        self.healthy()
        self.fixture["hold_probe"] = True
        self.sync_fixture()
        self.install_script_copy_tools()
        ready = self.stub_directory / "claim-ready"
        os.mkfifo(ready)
        stub = self.bin_directory / "command-stub"
        source = stub.read_text().replace('printf \'started\\n\' > "$root/probe-started"',
            'if [[ ${!#} == true ]]; then exit 0; fi\n'
            + 'printf \'started\\n\' > "$root/probe-started"')
        source = source.replace('if (( hold_probe )); then await_release continue-probe; fi',
            'if (( hold_probe )); then\n'
            + '    if [[ ! -f "$root/claim-ready-sent" ]]; then\n'
            + '        printf sent > "$root/claim-ready-sent"\n'
            + '        printf "ready\\n" > "$root/claim-ready"\n    fi\n'
            + '    await_release continue-probe\nfi')
        _ = stub.write_text(source)
        start_recovery = build_targets.start_probe_recovery
        recovery_started = False

        def observe_recovery(target: target_config.SshTarget, repo_root: Path,
                             package: str, kind: build_targets.StepKind) -> None:
            nonlocal recovery_started
            recovery_started = True
            start_recovery(target, repo_root, package, kind)
            # Force the old ordering: the probe owns the claim before eligibility returns.
            with ready.open() as signal:
                self.assertEqual(signal.readline(), "ready\n")

        args = build_targets.CliArguments()
        args.target, args.kind, args.repo_root, args.package = "box", "test", str(self.repository), "hana"
        args.pid, args.new_step = os.getpid(), True
        args.reservation_file = str(self.root / "pinned-reservation")
        output = io.StringIO()
        try:
            with mock.patch.object(build_targets, "start_probe_recovery", new=observe_recovery), redirect_stdout(output):
                self.assertEqual(build_targets.command_pinned_eligible(args, targets.ANY_ELIGIBLE_TARGET), 0)
            self.assertEqual(mac_test.read_claims(mac_test.state_paths("box")), ())
            self.assertEqual(build_targets.command_reserve(args), 0)
            self.assertIn(f"box\teligible\t-\t{self.linux_settings}\tlinux\n", output.getvalue())
            claim, = mac_test.read_claims(mac_test.state_paths("box"))
            self.assertEqual(claim.record["pid"], os.getpid())
            self.assertEqual(Path(args.reservation_file).read_text(), f"box\t{self.linux_settings}\n")
        finally:
            if recovery_started:
                with (self.stub_directory / "continue-probe").open("w") as release:
                    _ = release.write("continue\ncontinue\ncontinue\n")
                with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.release_reservation(), 0)

    def test_pinned_eligibility_repeated_in_a_wait_starts_no_recovery(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        self.install_script_copy_tools()
        args = build_targets.CliArguments()
        args.target, args.kind, args.repo_root, args.package = "box", "test", str(self.repository), "hana"
        self.clear_events()
        for attempt in range(2):
            with self.subTest(attempt=attempt), redirect_stdout(io.StringIO()):
                self.assertEqual(build_targets.command_pinned_eligible(args, targets.ANY_ELIGIBLE_TARGET), 0)
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.free(), "bad")

    def test_mac_version_decline_clears_when_the_next_run_matches(self) -> None:
        self.fixture["run_lines"] = [{"stream": "stdout", "text": "mac version mismatch: nextest differs"}]
        self.fixture["run_status_reported"] = False
        self.fixture["run_ssh_status"] = 1
        self.sync_fixture()
        result = self.command()
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertEqual(self.free("mac"), "bad")
        self.assertIsInstance(eligibility.read_backoff(time.time()), eligibility.NoBackoff)

        self.fixture["run_lines"] = []
        self.fixture["run_status_reported"] = True
        self.fixture["run_ssh_status"] = 0
        self.sync_fixture()
        passed = self.command()
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertEqual(self.free("mac"), "free")

    def test_busy_target_skips_recovery_and_keeps_the_mark(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        self.assertEqual(self.reserve(new_step=True), 0)
        count = len(self.events())
        target = target_config.configured_ssh_target("box")
        result = build_targets.ssh_target_eligibility(target, self.repository, "hana", "test",
                                                     targets.ANY_ELIGIBLE_TARGET, new_step=True)
        self.assertIsInstance(result, targets.ExcludedTarget)
        self.assertEqual(len(self.events()), count)
        self.assertIsInstance(eligibility.read_failed_probe("box"), eligibility.FailedProbe)
        self.assertEqual(self.release_reservation(), 0)

    def install_probe_gate(self) -> Path:
        ready = self.stub_directory / "recovery-ready"
        os.mkfifo(ready)
        stub = self.bin_directory / "command-stub"
        source = stub.read_text().replace('if (( hold_probe )); then await_release continue-probe; fi', r'''
if (( hold_probe )) && [[ ${!#} != true && ${!#} != printf* ]]; then
    if mkdir "$root/probe-gated" 2>/dev/null; then
        printf 'ready\n' > "$root/recovery-ready"
        await_release continue-probe
    fi
fi
''')
        _ = stub.write_text(source)
        self.fixture["hold_probe"] = True
        self.sync_fixture()
        return ready

    def finish_recovery(self, target: str = "box") -> None:
        with (self.stub_directory / "continue-probe").open("w") as release:
            _ = release.write("continue\n")
        with (eligibility.state_directory(target) / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)

    def test_mac_eligibility_uses_its_first_capacity_answer_during_recovery(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe("mac"), offload.UnavailableProbe)
        self.healthy()
        self.config.update({f"parity.{name}": "fixture" for name in ("rustc", "nextest", "mend", "scripts_commit")})
        self.sync_config()
        self.install_script_copy_tools()
        ready = self.install_probe_gate()
        start_recovery = build_targets.start_probe_recovery

        def claim_before_return(target: target_config.SshTarget, repo_root: Path,
                                package: str, kind: build_targets.StepKind) -> None:
            start_recovery(target, repo_root, package, kind)
            with ready.open() as signal:
                self.assertEqual(signal.readline(), "ready\n")

        self.clear_events()
        try:
            with mock.patch.object(build_targets, "start_probe_recovery", new=claim_before_return):
                result = build_targets.ssh_target_eligibility(target_config.configured_ssh_target("mac"),
                    self.repository, "hana", "test", targets.ANY_ELIGIBLE_TARGET, new_step=True)
            self.assertIsInstance(result, targets.EligibleTarget)
            probes = [event for event in self.events_for("ssh")
                      if not event["args"][-1].startswith("printf") and event["args"][-1] != "true"]
            self.assertEqual(len(probes), 1)
            self.assertEqual(self.free("mac"), "busy")
            self.assertIsInstance(eligibility.read_failed_probe(), eligibility.FailedProbe)
        finally:
            self.finish_recovery("mac")
        self.assertEqual(self.free("mac"), "free")

    def test_stopped_recovery_releases_its_claim_and_the_next_step_can_recover(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        self.install_script_copy_tools()
        ready = self.install_probe_gate()
        reaped = threading.Event()
        statuses: list[tuple[int, int]] = []
        real_waitpid = os.waitpid

        def observe_reap(pid: int, options: int) -> tuple[int, int]:
            result = real_waitpid(pid, options)
            if threading.current_thread().name == "probe-recovery-reaper":
                statuses.append(result)
                reaped.set()
            return result

        _ = self.enterContext(mock.patch.object(os, "waitpid", side_effect=observe_reap))
        build_targets.start_probe_recovery(target_config.configured_ssh_target("box"), self.repository, "hana", "test")
        with ready.open() as notification:
            self.assertEqual(notification.readline(), "ready\n")
        claim, = mac_test.read_claims(mac_test.state_paths("box"))

        def stop_recovery_group() -> None:
            try:
                send_process_group(claim.record["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass

        self.addCleanup(stop_recovery_group)
        os.kill(claim.record["pid"], signal.SIGTERM)
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("box")), ())
        self.assertEqual(self.free(), "bad")
        self.assertTrue(reaped.wait(5), "the recovery child was not reaped")
        pid, status = statuses[0]
        self.assertEqual(pid, claim.record["pid"])
        self.assertEqual(os.waitstatus_to_exitcode(status), 143)
        result = build_targets.ssh_target_eligibility(target_config.configured_ssh_target("box"),
            self.repository, "hana", "test", targets.ANY_ELIGIBLE_TARGET, new_step=True)
        self.assertIsInstance(result, targets.EligibleTarget)
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertEqual(self.free(), "free")

    def test_recovery_preserves_the_repository_decline_when_tools_still_differ(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        offload.write_version_decline("box", "hana")
        record = eligibility.linux_version_decline_path("box", "hana")
        previous = record.read_text()
        self.healthy()
        self.fixture["probe_lines"][-3] = "nextest=wrong"
        self.sync_fixture()
        self.install_script_copy_tools()
        result = build_targets.ssh_target_eligibility(target_config.configured_ssh_target("box"),
            self.repository, "hana", "test", targets.ANY_ELIGIBLE_TARGET, new_step=True)
        self.assertIsInstance(result, targets.EligibleTarget)
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertEqual(record.read_text(), previous)
        self.assertIsInstance(eligibility.read_failed_probe("box"), eligibility.FailedProbe)

    def test_recovery_copies_an_absent_mirror_before_the_real_linux_shell_probe(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        self.install_script_copy_tools()
        _ = (self.repository / "flake.nix").write_text("{ outputs = _: {}; }\n")
        _ = (self.repository / "flake.lock").write_text("{}\n")
        _ = write_config_fixture(self.linux_settings,
            "shell_cache=.local/state/probe-shells\ndevshell=default\n", template="mac_test")
        home = Path(self.environment["HOME"])
        _ = copy_helpers(home, *COPY_HELPERS)
        for name, program in {
            "nix": '#!/bin/sh\nprintf "export IN_NIX_SHELL=1\\n"\n',
            "cargo-nextest": '#!/bin/sh\nprintf "fixture\\n"\n',
            "cargo-mend": '#!/bin/sh\nprintf "fixture\\n"\n',
        }.items():
            path = self.bin_directory / name
            _ = path.write_text(program)
            path.chmod(0o755)
        ssh = self.bin_directory / "ssh"
        ssh.unlink()
        self.write_executable(ssh, r'''
import os
from pathlib import Path
import shlex
import subprocess
import sys
command = sys.argv[-1]
if command == "true":
    raise SystemExit(0)
if command.startswith("printf"):
    raise SystemExit(subprocess.run(["bash", "-c", command], check=False).returncode)
words = shlex.split(command)
if "dispatch_start_settled=" in command and "leftovers=0" in command:
    raise SystemExit(subprocess.run(["bash", "-c", command], cwd=Path.home(), check=False).returncode)
if "probe" in words and any("linux_runner.py" in word for word in words):
    raise SystemExit(subprocess.run(["bash", "-c", command], cwd=Path.home(), check=False).returncode)
raise SystemExit("Unexpected remote command: " + command)
''')
        result = build_targets.ssh_target_eligibility(target_config.configured_ssh_target("box"),
            self.repository, "hana", "test", targets.LocalRequired("local fixture tools"), new_step=True)
        self.assertIsInstance(result, targets.ExcludedTarget)
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertEqual(self.free(), "free")
        mirror = home / offload.remote_mirror(self.identity, "box")
        self.assertEqual((mirror / "flake.nix").read_text(), (self.repository / "flake.nix").read_text())
        self.assertEqual((mirror / "flake.lock").read_text(), (self.repository / "flake.lock").read_text())
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("box")), ())


class StepRecoveryTests(VerifyLintScopeFixture):
    def test_each_compiling_lint_command_has_one_placement_episode(self) -> None:
        settings = self.root / "mac_test.conf"
        _ = write_config_fixture(settings, f"offload=on\nlinux_only.{self.root.name}=\n")
        _ = (self.root / "build_targets.conf").write_text(
            "targets=natedev,mac\nkind.natedev=local\nplatform.natedev=linux\n"
            + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n" + UNREACHABLE_WAIT_FIXTURE)
        state = self.root / "mac-state"
        state.mkdir()
        _ = (state / "failed_probe").write_text("failed\n")
        admission = caller_admission(self, self.root, self.environment)
        requests: list[wire.PlacementRequest] = []
        previous = admission.server.handle_request

        def observe(request: wire.Request,
                    connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            if isinstance(request, wire.PlacementRequest):
                requests.append(request)
            return previous(request, connection)

        _ = self.enterContext(mock.patch.object(admission.server, "handle_request", side_effect=observe))
        self.environment.update({"BUILD_TARGETS_SCRIPT": str(Path(build_targets.__file__)),
                                 "MAC_TEST_STATE_DIR": str(state)})
        self.write_config(clippy="on")
        # A held recovery lock keeps the mark while the real local steps run.
        with (state / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.verify("lint", "sample", "--local", "local fixture tools")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        episodes = {request.episode: request.constraints.command for request in requests}
        local_steps = [row for row in self.cargo_calls()
                       if row[:1] in (["mend"], ["+nightly"], ["clippy"]) and "--version" not in row]
        taplo_steps = [json.loads(line) for line in self.taplo_events.read_text().splitlines()]
        self.assertGreaterEqual(len(local_steps), 2)
        self.assertTrue(taplo_steps)
        for name in ("mend", "clippy"):
            expected = [row for row in local_steps if row[0] == name]
            placed = [command for command in episodes.values() if name in command]
            self.assertEqual(len(placed), len(expected), (name, episodes, local_steps))
            self.assertEqual(len(placed), 1, (name, episodes))
        self.assertEqual(len(episodes), 2, episodes)
        self.assertEqual([request for request in requests if "fmt" in request.constraints.command], [])
        self.assertEqual([request for request in requests if "taplo" in request.constraints.command], [])
        self.assertEqual(list((self.root / "mac-state/claims").glob("*.json")), [])

    def test_local_work_finishes_while_a_new_step_recovers_linux(self) -> None:
        _ = write_config_fixture(self.root / "mac_test.conf", f"offload=on\nlinux_only.{self.root.name}=\n")
        _ = write_config_fixture(self.root / "linux.conf",
            "offload=on\nhost=fake-linux\nci_repo=\nkinds=test,check,doc,lint\n"
            + "devshell=default\nshell_cache=.local/state/build-target/shells\nshell_timeout_s=2\n"
            + "parity.rustc=fixture\nparity.nextest=fixture\nparity.mend=fixture\n"
            + "parity.scripts_commit=fixture\n", template="mac_test")
        _ = (self.root / "build_targets.conf").write_text(
            "targets=mac,natedev,linux\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\nkind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux.conf\n"
            + UNREACHABLE_WAIT_FIXTURE)
        state = Path(self.environment["HOME"]) / ".local/state/build-targets/linux"
        state.mkdir(parents=True)
        book = ledger.CostLedger(state)
        observed = int(time.time())
        book.record_inventory(ledger.ResourceInventory(observed, (
            ledger.PricedResource("retained-storage", Decimal("1")),), "private priced inventory"))
        book.observe(ledger.InstanceObservation(observed, "stopped", "fake DescribeInstances"))
        _ = (state / "failed_probe").write_text("failed\n")
        _ = (self.root / "remote-stamp").write_text("old")
        gate = self.root / "probe-release"
        os.mkfifo(gate)
        descriptor = os.open(gate, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, descriptor)
        git = self.root / "bin/git"
        source = git.read_text()
        prefix = ('case " $* " in\n'
            + '    *" ls-files -z scripts config "*) printf "scripts/mac_test/linux_runner.py\\0"; exit 0 ;;\n'
            + '    *" rev-parse HEAD "*) printf "fixture\\n"; exit 0 ;;\n'
            + '    *" diff HEAD --quiet -- scripts/mac_test/linux_runner.py "*) exit 0 ;;\nesac\n')
        _ = git.write_text(source.replace('#!/bin/sh\n', '#!/bin/sh\n' + prefix, 1)
            if source.startswith('#!/bin/sh\n') else '#!/bin/bash\n' + prefix + source.removeprefix('#!/bin/bash\n'))
        self.write_executable(self.root / "bin/rsync",
            'import json, os, sys\nfrom pathlib import Path\n'
            + 'with Path(os.environ["PACKAGE_ROOT"], "script-copies").open("a") as output:\n'
            + '    output.write(json.dumps(sys.argv[1:]) + "\\n")\n')
        self.write_executable(self.root / "bin/ssh", r'''
import json
import os
from pathlib import Path
import subprocess
import sys
root = Path(os.environ["PACKAGE_ROOT"])
command = sys.argv[-1]
if command == "true":
    raise SystemExit(0)
if command.startswith("printf") and ".build-target-script-commit" in command:
    (root / "remote-stamp").write_text("fixture")
    raise SystemExit(0)
if "dispatch_start_settled=" in command and "leftovers=0" in command:
    raise SystemExit(subprocess.run(["bash", "-c", command], cwd=Path.home(), check=False).returncode)
if "linux_runner.py" in command and "probe" in command:
    with (root / "recovery-probes").open("a") as output:
        output.write(json.dumps(sys.argv[1:]) + "\n")
    with (root / "probe-release").open() as release:
        assert release.readline() == "continue\n"
    print("free_gib=1000\nhost=fake-linux\nnextest=fixture\nmend=fixture\nrc=0")
    print("scripts_commit=" + (root / "remote-stamp").read_text())
    raise SystemExit(0)
print("fake Mac unavailable", file=sys.stderr)
raise SystemExit(255)
''')
        self.write_config(clippy="on")
        self.environment["BUILD_TARGETS_SCRIPT"] = str(Path(build_targets.__file__))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment, clear=True)))
        admission = caller_admission(self, self.root, self.environment)
        admission.server.host_probe = admission.server.read_provider_evidence
        result = self.verify("lint", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(args[:1] == ["clippy"] for args in self.cargo_calls()))
        # The remote probe remains blocked after local work finishes.
        with mock.patch.dict(os.environ, self.environment):
            self.assertIsInstance(eligibility.read_failed_probe("linux"), eligibility.FailedProbe)
        _ = os.write(descriptor, b"continue\n")
        with (state / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        with mock.patch.dict(os.environ, self.environment):
            self.assertEqual(mac_test.free_now(mac_test.state_paths("linux"),
                self.root / "linux.conf", self.root), "free")
            answer = build_targets.ssh_target_eligibility(target_config.configured_ssh_target("linux"),
                self.root, "sample", "lint", targets.ANY_ELIGIBLE_TARGET)
        self.assertIsInstance(answer, targets.EligibleTarget)
        probes = (self.root / "recovery-probes").read_text().splitlines()
        self.assertEqual(len(probes), 1)
        copies = [cast(list[str], json.loads(line)) for line in (self.root / "script-copies").read_text().splitlines()]
        self.assertEqual(len(copies), 2)
        self.assertTrue(any(word.startswith("--files-from=") for word in copies[0]))
        self.assertIn("--delete", copies[1])


if __name__ == "__main__":
    _ = unittest.main()

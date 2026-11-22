"""Check mac-linux claims against the native Mac claim path."""
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import final, override
import unittest
from unittest import mock

from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import mac_run
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, StepJob
from scripts.mac_test import claim_state, mac_test
from scripts.production.gate_routes import SuiteMode


@final
class MacRunTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.repo = Path()
        self.checkout = Path()
        self.paths = claim_state.StatePaths(Path())

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.repository(".claude")
        _ = subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
                            "commit", "-qm", "Fixture", "--allow-empty"], check=True)
        self.checkout = self.root / "checkout"
        _ = subprocess.run(["git", "clone", "-q", str(self.repo), str(self.checkout)], check=True)
        _ = subprocess.run(["git", "-C", str(self.checkout), "remote", "set-url", "origin",
                            "https://github.com/hanallc/claude_commands.git"], check=True)
        _ = (self.root / "machines.conf").write_text("mac.hosts=fixture-mac\n")
        _ = (self.root / "build_targets.conf").write_text("targets=natedev,mac\nkind.natedev=local\n"
                                                        + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n")
        _ = (self.root / "mac_test.conf").write_text("offload=on\nslots.claude_commands=99\n")
        self.configure_limit(1)
        self.enterContext(mock.patch.dict(os.environ, {
            "CONF_DIR": str(self.root), "MAC_TEST_STATE_DIR": str(self.root / "state"),
        }))
        _ = self.enterContext(mock.patch.object(claim_state, "submitting_session", return_value=NoOwningSession()))
        _ = self.enterContext(redirect_stdout(StringIO()))
        self.paths = claim_state.state_paths("mac")

    def repository(self, name: str) -> Path:
        path = self.root / name
        _ = subprocess.run(["git", "init", "-q", str(path)], check=True)
        return path

    def configure_limit(self, maximum: int) -> None:
        _ = (self.root / "build_folders.conf").write_text(
            f"# whole: 1..\nslots.fixture-mac.claude_commands.max={maximum}\n"
            + "# whole: 1..\nslots.fixture-mac.default_max=1\n")

    def native_claim(self, repo: Path) -> mac_test.CliArguments:
        args = mac_test.CliArguments()
        args.pid, args.worktree, args.what = os.getpid(), str(repo), "native Mac run"
        args.claim_id, args.episode, args.attempt = repo.name, "native-episode", "native-attempt"
        return args

    def test_guest_and_native_share_one_claim_and_another_repository_can_run(self) -> None:
        with ExitStack() as guest:
            _ = guest.enter_context(mac_run.held(self.checkout, "mac", SuiteMode.GATE, StepJob("test-all")))
            claim, = claim_state.read_claims(self.paths)
            self.assertEqual(claim.scope, claim_state.RepositoryScope(".claude", slot=1))
            self.assertEqual(claim.record["what"], "mac-linux test-all")
            self.assertEqual(claim.record["worktree"], str(self.checkout))
            with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with mac-linux test-all"):
                with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
                    self.fail("A second guest run took the same place")

            other = self.native_claim(self.repository("hana"))
            self.assertEqual(mac_test.command_claim(other, self.paths, watch_claim=False), 0)
            self.assertEqual(mac_test.command_release(other, self.paths), 0)

            native = self.native_claim(self.checkout)
            self.assertEqual(mac_test.free_now(self.paths, self.root / "mac_test.conf", self.checkout), "busy")
            self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 11)
            native.wait = 10

            def release_guest(_seconds: float) -> None:
                self.assertEqual(claim_state.read_claims(self.paths), (claim,))
                guest.close()

            with mock.patch.object(time, "sleep", side_effect=release_guest) as waited:
                self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
            waited.assert_called_once()

        with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with native Mac run"):
            with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
                self.fail("The guest took the native run's place")
        self.assertEqual(mac_test.command_release(native, self.paths), 0)
        self.assertEqual(claim_state.read_claims(self.paths), ())

    def test_claim_count_uses_the_repository_limit_and_host_default(self) -> None:
        self.configure_limit(2)
        scope = claim_state.RepositoryScope(".claude")
        self.assertEqual(mac_test.slot_capacity(self.paths, scope), 2)
        self.assertEqual(mac_test.slot_capacity(self.paths, claim_state.RepositoryScope("hana")), 1)
        for first in ("native", "guest"):
            with self.subTest(first=first), ExitStack() as claims:
                native = self.native_claim(self.repo)

                def start_native() -> None:
                    self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
                    _ = claims.callback(mac_test.command_release, native, self.paths)

                if first == "native":
                    start_native()
                _ = claims.enter_context(mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED))
                if first == "guest":
                    guest, = claim_state.read_claims(self.paths)
                    self.assertEqual(guest.scope, claim_state.RepositoryScope(".claude", slot=2))
                    start_native()
                self.assertEqual({claim.scope for claim in claim_state.read_claims(self.paths)},
                                 {scope, claim_state.RepositoryScope(".claude", slot=2)})
                with self.assertRaises(mac_run.MacRunRefused):
                    with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
                        self.fail("A third run exceeded the configured limit")
            self.assertEqual(claim_state.read_claims(self.paths), ())

    def test_native_starts_when_either_guest_releases_a_slot(self) -> None:
        self.configure_limit(2)
        settings = self.root / "mac_test.conf"
        for ends_first in (0, 1):
            with self.subTest(ends_first=ends_first), ExitStack() as claims:
                guests = [claims.enter_context(ExitStack()) for _ in range(2)]
                for guest in guests:
                    _ = guest.enter_context(mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED))
                self.assertEqual({claim.scope for claim in claim_state.read_claims(self.paths)},
                                 {claim_state.RepositoryScope(".claude", slot=slot) for slot in (1, 2)})
                native = self.native_claim(self.checkout)
                self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout), "busy")
                self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 11)

                guests[ends_first].close()
                self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout), "free")
                self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
                _ = claims.callback(mac_test.command_release, native, self.paths)
                self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout, mac_kind=mac_test.MacClaimKind.GUEST),
                                 "busy")

                guests[1 - ends_first].close()
                self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout), "busy")
                self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout, mac_kind=mac_test.MacClaimKind.GUEST),
                                 "free")
                with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
                    self.assertEqual(len(claim_state.read_claims(self.paths)), 2)
            self.assertEqual(claim_state.read_claims(self.paths), ())

    def test_numbered_claim_cannot_replace_a_repository_with_a_slot_suffix(self) -> None:
        with (self.root / "build_folders.conf").open("a") as settings:
            _ = settings.write("# whole: 1..\nslots.fixture-mac.hana.max=2\n")
        other = self.native_claim(self.repository("hana.slot-2"))
        self.addCleanup(mac_test.command_release, other, self.paths)
        self.assertEqual(mac_test.command_claim(other, self.paths, watch_claim=False), 0)
        original, = claim_state.read_claims(self.paths)
        original_data = original.path.read_bytes()

        with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, repository="hana"):
            claims = {claim.scope: claim for claim in claim_state.read_claims(self.paths)}
            numbered = claims[claim_state.RepositoryScope("hana", slot=2)]
            self.assertEqual(claims[claim_state.RepositoryScope("hana.slot-2")], original)
            self.assertNotEqual(numbered.path, original.path)
            self.assertEqual(original.path.read_bytes(), original_data)
        self.assertEqual(claim_state.read_claims(self.paths), (original,))

    def test_legacy_numbered_claim_keeps_its_file_until_release(self) -> None:
        with (self.root / "build_folders.conf").open("a") as settings:
            _ = settings.write("# whole: 1..\nslots.fixture-mac.hana.max=2\n")
        other = self.native_claim(self.repository("hana.slot-2"))
        with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, repository="hana"):
            claim, = claim_state.read_claims(self.paths)
            legacy = claim.path.with_name("hana.slot-2.json")
            _ = claim.path.rename(legacy)
            original_data = legacy.read_bytes()
            self.assertEqual(mac_test.command_claim(other, self.paths, watch_claim=False), 11)
            self.assertEqual(mac_test.free_now(self.paths, self.root / "mac_test.conf", Path(other.worktree)), "busy")
            self.assertEqual(legacy.read_bytes(), original_data)
        self.assertEqual(mac_test.command_claim(other, self.paths, watch_claim=False), 0)
        self.assertEqual(mac_test.command_release(other, self.paths), 0)

    def test_claim_refusal_reaches_the_guest_without_printing(self) -> None:
        self.paths.directory.mkdir()
        _ = self.paths.block.write_text("invalid block\n")
        output = StringIO()
        with redirect_stdout(output), self.assertRaisesRegex(mac_run.MacRunRefused, "state unreadable"):
            with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
                self.fail("A guest started with unreadable state")
        self.assertEqual(output.getvalue(), "")

    def test_mac_activity_watcher_leaves_guest_claim_for_its_holder(self) -> None:
        with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
            claim, = claim_state.read_claims(self.paths)
            config = mac_test.MacTestConfig("", "", "", "", 1, 1, 1, 1, 1)
            with mock.patch.object(mac_test, "check_mac_activity", return_value=mac_test.MacIdle()) as probe:
                self.assertFalse(mac_test.claim_watch_pass(self.paths, config))
            probe.assert_not_called()
            self.assertEqual(claim_state.read_claims(self.paths), (claim,))
        self.assertEqual(claim_state.read_claims(self.paths), ())

    def test_release_errors_reach_the_caller_after_the_guest_run(self) -> None:
        refused = mac_test.ClaimReleaseRefused("the exact claim changed during cleanup")
        with mock.patch.object(mac_test, "release_exact_claims", return_value=refused):
            with self.assertRaisesRegex(RuntimeError, refused.reason):
                with mac_run.held(self.checkout, "mac", StepJob("test-affected"), GATE_JOB_NOT_RECORDED):
                    self.assertEqual(len(claim_state.read_claims(self.paths)), 1)

    def test_release_error_preserves_the_guest_error(self) -> None:
        refused = mac_test.ClaimReleaseRefused("the exact claim changed during cleanup")
        with mock.patch.object(mac_test, "release_exact_claims", return_value=refused):
            with self.assertRaisesRegex(ValueError, "guest failed") as failed:
                with mac_run.held(self.checkout, "mac", StepJob("test-affected"), GATE_JOB_NOT_RECORDED):
                    raise ValueError("guest failed")
        self.assertEqual(failed.exception.__notes__, [f"Mac claim release failed: {refused.reason}"])

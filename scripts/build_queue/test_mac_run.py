"""Check mac-linux claims against the native Mac claim path."""
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
import errno
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from threading import Event
import time
from typing import TypedDict, cast, final, override
import unittest
from unittest import mock

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_queue import execution_identity, mac_run, placement, step_estimator
from scripts.build_queue.admission_records import RepositoryIdentity
from scripts.build_queue import admission_wire as wire
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, StepJob
from scripts.buildlog import repository, store, sync as buildlog_sync
from scripts.build_targets import build_targets, mac_linux, target_config
from scripts.copies import copies
from scripts.direct import phase_wait_holder
from scripts.lib import conf, process_start as process_identity
from scripts.mac_test import claim_state, eligibility, mac_queue_run, mac_test, offload
from scripts.production.gate_routes import SuiteMode


ORIGIN = "https://github.com/hanallc/claude_commands.git"
REPOSITORY_KEY = ORIGIN.removesuffix(".git")
HANA_ORIGIN = "https://fixture.invalid/team/hana.git"


class FixtureArmedGuestRecord(TypedDict):
    attempt: str
    boot_id: str
    deadline_ns: int
    machine_id: str


@dataclass(frozen=True)
class FixtureArmedGuestAttempt:
    attempt: str
    boot_id: str
    deadline_ns: int
    machine_id: str

    def to_record(self) -> FixtureArmedGuestRecord:
        return {"attempt": self.attempt, "boot_id": self.boot_id,
                "deadline_ns": self.deadline_ns, "machine_id": self.machine_id}

    @classmethod
    def from_record(cls, record: FixtureArmedGuestRecord) -> "FixtureArmedGuestAttempt":
        try:
            return cls(**record)
        except TypeError as error:
            raise ValueError("The armed guest record is invalid") from error


ARMED_GUEST_RECORD: FixtureArmedGuestRecord = {
    "attempt": "fixture-guest",
    "boot_id": "e68d2026-ec82-4b04-bb90-67d1481df9e3",
    "deadline_ns": 123,
    "machine_id": "c2b039b345b84686bfe5d660ab70caba",
}


class FixtureGuestEndUnproven(mac_linux.GuestUnavailable):
    def __init__(self, message: str, armed: FixtureArmedGuestAttempt) -> None:
        self.armed: FixtureArmedGuestAttempt = armed
        super().__init__(message)


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
        self.enterContext(mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "0",
        }))
        self.repo = self.repository(".claude")
        _ = subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
                            "commit", "-qm", "Fixture", "--allow-empty"], check=True)
        _ = subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", ORIGIN], check=True)
        self.checkout = self.root / "checkout"
        _ = subprocess.run(["git", "clone", "-q", str(self.repo), str(self.checkout)], check=True)
        _ = subprocess.run(["git", "-C", str(self.checkout), "remote", "set-url", "origin",
                            ORIGIN], check=True)
        _ = (self.root / "machines.conf").write_text("mac.hosts=fixture-mac\n")
        _ = (self.root / "build_targets.conf").write_text("targets=natedev,mac\nkind.natedev=local\nplatform.natedev=linux\n"
                                                        + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n")
        _ = (self.root / "mac_test.conf").write_text("offload=on\nslots.claude_commands=99\n")
        copies_config = self.root / "copies.conf"
        _ = copies_config.write_text("# range: 5e-324..\ngit_timeout_seconds=5\n")
        self.configure_limit(1)
        self.enterContext(mock.patch.dict(os.environ, {
            "CONF_DIR": str(self.root), "COPIES_CONFIG": str(copies_config),
            "MAC_TEST_STATE_DIR": str(self.root / "state"),
        }))
        _ = self.enterContext(mock.patch.object(claim_state, "submitting_session", return_value=NoOwningSession()))
        _ = self.enterContext(mock.patch.object(copies, "remote_registry", return_value=self.root / "remote-registry"))
        _ = self.enterContext(redirect_stdout(StringIO()))
        self.paths = claim_state.state_paths("mac")
        if not hasattr(mac_linux, "prove_kept_guest_end"):
            _ = self.enterContext(mock.patch.object(mac_linux, "ArmedGuestAttempt", FixtureArmedGuestAttempt))
            _ = self.enterContext(mock.patch.object(mac_linux, "GuestEndUnproven", FixtureGuestEndUnproven, create=True))

    def repository(self, name: str, *, origin: str = "") -> Path:
        path = self.root / name
        _ = subprocess.run(["git", "init", "-q", str(path)], check=True)
        if origin:
            _ = subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
        return path

    def configure_limit(self, maximum: int) -> None:
        _ = (self.root / "build_folders.conf").write_text(
            f"# whole: 1..\nslots.fixture-mac.claude_commands.max={maximum}\n"
            + "# whole: 1..\nnames.repository_key_length=8\n"
            + "# whole: 1..\nslots.fixture-mac.default_max=1\n")

    def native_claim(self, repo: Path) -> mac_test.CliArguments:
        args = mac_test.CliArguments()
        args.pid, args.worktree, args.what = os.getpid(), str(repo), "native Mac run"
        args.claim_id, args.episode, args.attempt = repo.name, "native-episode", "native-attempt"
        return args

    def test_guest_and_native_share_one_claim_and_another_repository_can_run(self) -> None:
        with ExitStack() as guest:
            _ = guest.enter_context(mac_run.held(self.checkout, "mac", SuiteMode.GATE, StepJob("test-all"), attempt="fixture-attempt"))
            claim, = claim_state.read_claims(self.paths)
            self.assertEqual(claim.scope, claim_state.RepositoryScope("checkout", REPOSITORY_KEY, 1))
            self.assertEqual(claim.record["what"], "mac-linux test-all")
            self.assertEqual(claim.record["worktree"], str(self.checkout))
            with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with mac-linux test-all"):
                with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
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

            waited = mock.Mock(side_effect=release_guest)
            with mock.patch.object(mac_test, "time", mock.Mock(wraps=time, sleep=waited)):
                self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
            waited.assert_called_once()

        with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with native Mac run"):
            with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                self.fail("The guest took the native run's place")
        self.assertEqual(mac_test.command_release(native, self.paths), 0)
        self.assertEqual(claim_state.read_claims(self.paths), ())

    def test_quoted_origin_uses_the_shared_repository_scope(self) -> None:
        config = self.checkout / ".git/config"
        origin = ORIGIN
        _ = config.write_text(config.read_text().replace(f"url = {origin}", f'url = "{origin}"'))
        self.assertIn(f'url = "{origin}"', config.read_text())
        native = self.native_claim(self.repo)
        self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
        self.addCleanup(mac_test.command_release, native, self.paths)

        self.assertEqual(claim_state.target_claim_scope(self.paths, str(self.checkout)),
                         claim_state.RepositoryScope("checkout", REPOSITORY_KEY))
        settings = self.root / "mac_test.conf"
        self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout, mac_kind=mac_test.MacClaimKind.GUEST),
                         "busy")
        with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with native Mac run"):
            with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                self.fail("The quoted origin bypassed the native claim")

    def test_legacy_claim_blocks_native_and_guest_claims_with_or_without_its_checkout(self) -> None:
        old = self.native_claim(self.checkout)
        path = self.paths.claims / "checkout.json"
        path.parent.mkdir(parents=True)
        start = process_identity.process_start(old.pid)
        assert isinstance(start, process_identity.Started)
        old_record: claim_state.ProcessBoundClaim = {
            "pid": old.pid, "proc_start": start.start, "what": old.what, "worktree": str(self.checkout),
            "since": "2026-11-21T10:00:00+00:00", "claim_id": old.claim_id,
            "episode": old.episode, "attempt": old.attempt, "submitting_session": "", "repo_name": "checkout",
        }
        other = self.repository("hana", origin=HANA_ORIGIN)
        native = self.native_claim(other)
        settings = self.root / "mac_test.conf"
        for worktree in (self.checkout, self.root / "missing"):
            with self.subTest(worktree=worktree):
                old_record["worktree"] = str(worktree)
                if worktree != self.checkout:
                    old_record["repo_key"] = ""
                mac_test.write_json(path, old_record)
                saved = path.read_bytes()
                with mock.patch.object(subprocess, "run", side_effect=AssertionError("Claim readers must not run Git")):
                    claim, = claim_state.read_claims(self.paths)
                self.assertEqual(claim.scope, claim_state.WHOLE_MAC)
                self.assertEqual(mac_test.free_now(self.paths, settings, other), "busy")
                self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 11)
                self.assertEqual(mac_test.free_now(self.paths, settings, other, mac_kind=mac_test.MacClaimKind.GUEST),
                                 "busy")
                with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with native Mac run"):
                    with mac_run.held(other, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                        self.fail("The guest bypassed the saved claim")
                self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(mac_test.command_release(old, self.paths), 0)
        self.assertEqual(claim_state.read_claims(self.paths), ())

    def test_legacy_assignment_saves_a_key_before_acquiring_the_lock(self) -> None:
        old_scope = claim_state.RepositoryScope("checkout")
        waiting = self.native_claim(self.checkout)
        start = process_identity.process_start(waiting.pid)
        assert isinstance(start, process_identity.Started)
        assignment = placement.PlacementAssignment("saved-episode", "saved-attempt",
            placement.PlacementDestination("mac", "mac", "fixture-mac", old_scope),
            execution_identity.SourceKeyUnavailable("The fixture has no source key"),
            placement.SubmittingOwner("fixture-host", waiting.pid, start.start), NoOwningSession(), 0)
        with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
            self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False, assignment=assignment), 11)

        resolve = repository.mac_source_repository

        def resolve_outside_lock(worktree: str, common: str
                                 ) -> repository.MacSourceRepository | repository.UnrecordedSourceCheckout:
            self.assertEqual(claim_state.coordination_depth.get(), 0)
            return resolve(worktree, common)

        with mock.patch.object(repository, "mac_source_repository", side_effect=resolve_outside_lock) as lookup:
            self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False, assignment=assignment), 0)
        lookup.assert_called_once()
        self.addCleanup(mac_test.command_release, waiting, self.paths)
        reserved, = claim_state.read_claims(self.paths)
        self.assertEqual(reserved.scope, claim_state.RepositoryScope("checkout", REPOSITORY_KEY))
        self.assertTrue(reserved.matches_mac_assignment_scope(old_scope))

    def test_mac_assignment_accepts_the_saved_github_key_in_mixed_case(self) -> None:
        scope = claim_state.RepositoryScope("checkout", "https://github.com/HanaLLC/Claude_Commands")
        waiter = self.native_claim(self.checkout)
        start = process_identity.process_start(waiter.pid)
        assert isinstance(start, process_identity.Started)
        assignment = placement.PlacementAssignment("saved-episode", "saved-attempt",
            placement.PlacementDestination("mac", "mac", "fixture-mac", scope),
            execution_identity.SourceKeyUnavailable("The fixture has no source key"),
            placement.SubmittingOwner("fixture-host", waiter.pid, start.start), NoOwningSession(), 0)

        self.assertEqual(mac_test.command_claim(waiter, self.paths, watch_claim=False, assignment=assignment), 0)
        reserved, = claim_state.read_claims(self.paths)
        self.assertEqual(reserved.record.get("repo_key"), REPOSITORY_KEY)
        self.assertTrue(reserved.matches_mac_assignment_scope(scope))

    def test_kept_claim_takeover_and_cleanup_use_the_saved_key(self) -> None:
        with (self.root / "mac_test.conf").open("a") as settings:
            _ = settings.write("mirror_root=~/.local/state/mac-test/mirror\n")
        old = self.native_claim(self.repo)
        old.remote = "fixture-mac"
        with mock.patch.object(claim_state, "submitting_session", return_value=OwningSession("old-owner")):
            self.assertEqual(mac_test.command_claim(old, self.paths, watch_claim=False), 0)
        held, = claim_state.read_claims(self.paths)
        assert "remote_host" in held.record
        waiting = self.native_claim(self.checkout)
        waiting.remote = "fixture-mac"
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "waiting-owner"}):
            self.assertEqual(phase_wait_holder.mac_owner("mac", str(self.checkout)), ("--holder-session", "old-owner"))
        mac_test.write_json(held.path, {**held.record, "proc_start": "ended", "worktree": "/missing/checkout"})

        self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False), 13)
        kept, = claim_state.read_claims(self.paths)
        self.assertEqual(kept.record.get("repo_key"), REPOSITORY_KEY)
        self.assertEqual(claim_state.claim_identity(kept.record), claim_state.claim_identity(held.record))
        with mock.patch.object(eligibility, "read_connection_settings"), \
                mock.patch.object(offload, "check_leftovers", return_value=offload.LeftoversEnded()) as check:
            args = build_targets.CliArguments()
            args.pid, args.repo_root = waiting.pid, str(self.checkout)
            target = target_config.SshTarget("mac", "macos", self.root / "mac_test.conf")
            build_targets.cleanup_refused_claims(target, args, session=NoOwningSession())
        self.assertEqual(check.call_args.args[2], eligibility.KnownRepository(".claude", REPOSITORY_KEY))
        self.assertEqual(claim_state.read_claims(self.paths), ())

    def test_lost_takeover_reply_recovers_the_saved_claim_identity(self) -> None:
        original = self.native_claim(self.repo)
        original.remote = "original-mac"
        self.assertEqual(mac_test.command_claim(original, self.paths, watch_claim=False), 0)
        retained, = claim_state.read_claims(self.paths)
        mac_test.write_json(retained.path, {**retained.record, "proc_start": "ended"})
        start = process_identity.process_start(os.getpid())
        assert isinstance(start, process_identity.Started)
        assignment = placement.PlacementAssignment("new-episode", "new-attempt",
            placement.PlacementDestination("mac", "mac", "fixture-mac", retained.scope),
            execution_identity.SourceKeyUnavailable("The fixture has no source key"),
            placement.SubmittingOwner("fixture-host", os.getpid(), start.start), NoOwningSession(), 0)
        request = offload.OffloadRequest(self.checkout, "hana", "test", offload.NO_RESULT_FILE, (),
            "verify", "", target="mac", placement=offload.AssignedOffloadRequest(assignment))
        saved = claim_state.saved_mac_repository(retained.record)
        assert isinstance(saved, claim_state.MacRepositoryIdentity)
        config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("fixture-mac", 1, 1),
                                           1, 1, 1, "1", {"linux_only.checkout": ""})
        script = """
from contextlib import redirect_stdout
from io import StringIO
import sys
from scripts.mac_test import mac_test

args = mac_test.parser().parse_args(sys.argv[1:], namespace=mac_test.CliArguments())
with redirect_stdout(StringIO()):
    status = mac_test.command_claim(args, mac_test.state_paths("mac"), watch_claim=False, session=args.session)
assert status == 13, status
sys.exit(71)
"""
        command = offload.claim_arguments(request, config, mac_repository=saved)
        controller = offload.ProcessController()
        with mock.patch.object(offload, "claim_arguments", return_value=[sys.executable, "-c", script, *command[2:]]):
            result = offload.claim_for_run(controller, config, request,
                                           eligibility.KnownRepository(saved.mirror_name, saved.key),
                                           mac_repository=saved)
        current, = claim_state.read_claims(self.paths)
        self.assertEqual(current.record["pid"], os.getpid())
        self.assertEqual(result, offload.TakenOverMacClaim("original-mac", 1))
        self.assertEqual(controller.claim_identity, claim_state.claim_identity(retained.record))
        self.assertEqual(controller.claim_scope_key, claim_state.claim_fence_scope_key(retained.path, retained.record))
        self.assertEqual(controller.mac_repository, saved)
        unrelated = replace(assignment, attempt="other-attempt")
        controller = offload.ProcessController()
        controller.placement = offload.AssignedOffloadRequest(unrelated)
        inspection = offload.LocalClaimAttempt()
        controller.inspect_claim(inspection, claim_state.ClaimIdentity(
            execution_identity.assigned_claim_id(unrelated.episode, unrelated.attempt),
            unrelated.episode, unrelated.attempt))
        self.assertIsInstance(inspection.answer, offload.MacClaimNotConfirmed)

    def test_refused_claim_cleanup_uses_the_mirror_that_ran(self) -> None:
        config = self.root / "mac_test.conf"
        mirror_root = ".local/state/mac-test/test-mirror"
        with config.open("a") as settings:
            _ = settings.write(f"mirror_root=~/{mirror_root}\n")
        old = self.native_claim(self.checkout)
        old.remote = "fixture-mac"
        self.assertEqual(mac_test.command_claim(old, self.paths, watch_claim=False), 0)
        held, = claim_state.read_claims(self.paths)
        saved = claim_state.saved_mac_repository(held.record)
        assert isinstance(saved, claim_state.MacRepositoryIdentity)
        self.assertEqual(saved.mirror_name, "checkout")
        mac_test.write_json(held.path, {**held.record, "proc_start": "ended"})
        ran = self.root / offload.remote_mirror(eligibility.KnownRepository(saved.mirror_name, saved.key))
        other = self.root / mirror_root / ".claude"
        ran.mkdir(parents=True)
        other.mkdir()
        marker = self.root / "ended"
        owner = self.native_claim(self.repo)

        def local_ssh(_settings: eligibility.SshConnectionSettings, host: str,
                      script: str, target: str) -> offload.LeftoverObservation:
            self.assertEqual((host, target), ("fixture-mac", "mac"))
            fixture = (
                "pgrep() { return 1; }\n"
                + f"lsof() {{ if [ ! -e {shlex.quote(str(marker))} ]; then "
                + f"printf 'p41\\nn%s\\n' {shlex.quote(str(ran))}; fi; "
                + f"printf 'p42\\nn%s\\n' {shlex.quote(str(other))}; }}\n"
                + f"kill() {{ [ \"$1\" = 41 ] && touch {shlex.quote(str(marker))}; }}\n"
                + "sleep() { :; }\n"
            )
            result = subprocess.run(["bash", "-c", fixture + script], check=True, capture_output=True,
                                    text=True, timeout=5)
            return offload.LeftoverObservation(offload.parse_leftover_check(result.stdout), result.stdout)

        with mock.patch.object(eligibility, "read_connection_settings", return_value=eligibility.SshConnectionSettings(
                "replacement-mac", 1, 1)), mock.patch.object(offload, "read_leftovers", side_effect=local_ssh) as check:
            args = build_targets.CliArguments()
            args.pid, args.repo_root = owner.pid, str(self.repo)
            target = target_config.SshTarget("mac", "macos", config)
            before = held.path.read_bytes()
            with copies.remote_host_lease("fixture-mac", exclusive=True), \
                    mock.patch.object(mac_test, "command_release") as release:
                build_targets.cleanup_refused_claims(target, args, session=NoOwningSession())
            check.assert_not_called()
            release.assert_not_called()
            self.assertEqual(held.path.read_bytes(), before)
            build_targets.cleanup_refused_claims(target, args, session=NoOwningSession())
        self.assertTrue(marker.exists())
        self.assertEqual(claim_state.read_claims(self.paths), ())

    def test_refused_cleanup_continues_after_a_lease_open_error(self) -> None:
        settings = self.root / "mac_test.conf"
        with settings.open("a") as config:
            _ = config.write("mirror_root=~/.local/state/mac-test/mirror\n")
        start = process_identity.process_start(os.getpid())
        assert isinstance(start, process_identity.Started)
        self.paths.claims.mkdir(parents=True)
        first, second = (self.paths.claims / f"{name}.json" for name in ("first", "second"))
        for index, path in enumerate((first, second), 1):
            mac_test.write_json(path, {
                "claim_id": path.stem, "episode": "wait", "attempt": "attempt", "submitting_session": "",
                "pid": os.getpid(), "proc_start": f"{start.start}-ended", "what": "retained", "worktree": str(self.checkout),
                "since": f"2026-11-22T00:00:0{index}+00:00", "remote_host": path.stem,
            })
        registry = copies.remote_registry()
        lease = registry / (copies.remote_key("first", "") + ".host-lease")
        lease.mkdir(parents=True)
        before = first.read_bytes()
        args = build_targets.CliArguments()
        args.pid, args.repo_root = os.getpid(), str(self.checkout)
        target = target_config.SshTarget("mac", "macos", settings)
        config = eligibility.SshConnectionSettings("fixture-mac", 1, 1)
        diagnostic = StringIO()
        with mock.patch.object(eligibility, "read_connection_settings", return_value=config), \
                mock.patch.object(offload, "check_leftovers", return_value=offload.LeftoversEnded()) as check, \
                redirect_stderr(diagnostic):
            build_targets.cleanup_refused_claims(target, args, session=NoOwningSession())
        check.assert_called_once_with(config, "second", offload.ALL_MIRRORS, "mac", 1)
        self.assertIn(str(lease), diagnostic.getvalue())
        self.assertEqual(first.read_bytes(), before)
        self.assertFalse(second.exists())

    def test_linked_worktrees_with_a_relative_origin_share_capacity(self) -> None:
        _ = subprocess.run(["git", "-C", str(self.checkout), "remote", "set-url", "origin", "../.claude"], check=True)
        linked = self.root / "linked"
        linked.mkdir()
        worktrees = (self.root / "one", linked / "two")
        for worktree in worktrees:
            _ = subprocess.run(["git", "-C", str(self.checkout), "worktree", "add", "-q", "--detach",
                                str(worktree)], check=True)
        scopes = tuple(claim_state.target_claim_scope(self.paths, str(path)) for path in worktrees)
        self.assertEqual(scopes, (claim_state.RepositoryScope(".claude", REPOSITORY_KEY),) * 2)
        with mac_run.held(worktrees[0], "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
            with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with mac-linux"):
                with mac_run.held(worktrees[1], "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                    self.fail("The second guest took the shared slot")
        with mac_run.held(worktrees[1], "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
            self.assertEqual(len(claim_state.read_claims(self.paths)), 1)

    def test_mac_local_origins_follow_the_upstream_repository(self) -> None:
        source = claim_state.mac_repository_identity(str(self.repo))
        assert isinstance(source, claim_state.MacMirrorIdentity)
        self.assertEqual(source.key, REPOSITORY_KEY)
        for origin in (str(self.repo), "../.claude"):
            with self.subTest(origin=origin):
                _ = subprocess.run(["git", "-C", str(self.checkout), "remote", "set-url", "origin", origin], check=True)
                mirror = claim_state.mac_repository_identity(str(self.checkout))
                assert isinstance(mirror, claim_state.MacMirrorIdentity)
                self.assertEqual(mirror.record(), source.record())
        native = self.native_claim(self.repo)
        self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
        self.addCleanup(mac_test.command_release, native, self.paths)
        with self.assertRaises(mac_run.MacRunRefused), mac_run.held(
                self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
            self.fail("The clone exceeded the repository limit")

    def test_mac_capacity_uses_only_the_shared_git_origin_and_rewrites(self) -> None:
        linked = self.root / "linked"
        git = ["git", "-C", str(self.checkout)]
        origin = "shared:upstream/.claude"
        _ = subprocess.run([*git, "remote", "set-url", "origin", origin], check=True)
        _ = subprocess.run([*git, "config", "--local", "url.https://fixture.invalid/fallback/.insteadOf",
                            "shared:"], check=True)
        _ = subprocess.run([*git, "config", "--local", f"url.{self.root}/.insteadOf", "shared:upstream/"], check=True)
        _ = subprocess.run([*git, "config", "--local", "extensions.worktreeConfig", "true"], check=True)
        _ = subprocess.run([*git, "worktree", "add", "-q", "--detach", str(linked)], check=True)
        for checkout, key in (
            (self.checkout, "url.https://fixture.invalid/primary.git.insteadOf"),
            (linked, "url.https://fixture.invalid/linked.git.insteadOf"),
        ):
            worktree = ["git", "-C", str(checkout), "config", "--worktree"]
            _ = subprocess.run([*worktree, key, origin], check=True)
            _ = subprocess.run([*worktree, "remote.origin.url", "https://worktree.invalid/team/hana"], check=True)
        global_config = self.root / "global.gitconfig"
        _ = global_config.write_text('[includeIf "hasconfig:remote.*.url:https://worktree.invalid/**"]\n'
                                     + ' path = worktree-alias.gitconfig\n')
        _ = (self.root / "worktree-alias.gitconfig").write_text(
            f'[url "https://fixture.invalid/worktree.git"]\n insteadOf = {origin}\n')
        self.enterContext(mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_config)}))

        native = self.native_claim(self.repo)
        self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
        self.addCleanup(mac_test.command_release, native, self.paths)
        for checkout in (self.checkout, linked):
            with self.subTest(checkout=checkout.name):
                with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with native Mac run"):
                    with mac_run.held(checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                        self.fail("A worktree override bypassed the repository limit")
                self.assertEqual(claim_state.target_claim_scope(self.paths, str(checkout)),
                                 claim_state.RepositoryScope(".claude", REPOSITORY_KEY))

    def test_mac_capacity_shares_global_alias_and_ssh_https_origins(self) -> None:
        for scope in ("GLOBAL", "SYSTEM"):
            config = self.root / scope
            _ = config.write_text(f'[url "git@github.com:"]\n insteadOf = {scope.lower()}:\n')
            self.enterContext(mock.patch.dict(os.environ, {f"GIT_CONFIG_{scope}": str(config)}))
        upstream = self.repository("hana", origin="https://github.com/Team/Hana")
        native = self.native_claim(upstream)
        self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
        self.addCleanup(mac_test.command_release, native, self.paths)
        for name, origin in (
            ("case-only", "https://github.com/team/hana"),
            ("global-alias", "global:team/hana.git"),
            ("worktree-global-alias", "global:team/hana.git"),
            ("worktree-system-alias", "system:team/hana.git"),
            ("scp-url", "git@github.com:team/hana.git"),
            ("ssh-url", "ssh://git@github.com:22/team/hana.git"),
        ):
            with self.subTest(checkout=name):
                checkout = self.repository(name, origin=origin)
                if name.startswith("worktree-"):
                    git = ["git", "-C", str(checkout), "config"]
                    _ = subprocess.run([*git, "--local", "extensions.worktreeConfig", "true"], check=True)
                    _ = subprocess.run([*git, "--worktree", "url.git@worktree.invalid:.insteadOf", origin], check=True)
                self.assertEqual(claim_state.target_claim_scope(self.paths, str(checkout)),
                                 claim_state.RepositoryScope(name, "https://github.com/team/hana"))
                with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with native Mac run"):
                    with mac_run.held(checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                        self.fail("An alias bypassed the repository limit")

    def test_separate_git_directory_with_a_relative_origin_shares_capacity(self) -> None:
        separate = self.root / "separate"
        metadata = self.root / "metadata" / "hana" / "repo.git"
        metadata.parent.mkdir(parents=True)
        _ = subprocess.run(["git", "clone", "-q", "--separate-git-dir", str(metadata),
                            str(self.repo), str(separate)], check=True)
        for checkout, origin in ((separate, "../.claude"), (self.checkout, "../separate")):
            _ = subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "origin", origin], check=True)

        linked = self.root / "linked" / "worktree"
        peer = self.root / "peer" / "worktree"
        _ = subprocess.run(["git", "-C", str(separate), "config", "core.worktree", str(separate)], check=True)
        _ = subprocess.run(["git", "-C", str(separate), "worktree", "add", "-q", "--detach", str(linked)], check=True)
        _ = subprocess.run(["git", "-C", str(separate), "worktree", "add", "-q", "--detach", str(peer)], check=True)
        native = self.native_claim(self.repo)
        self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
        self.addCleanup(mac_test.command_release, native, self.paths)
        for checkout in (separate, self.checkout, linked):
            with self.subTest(checkout=checkout.name):
                self.assertEqual(claim_state.target_claim_scope(self.paths, str(checkout)),
                                 claim_state.RepositoryScope(".claude", REPOSITORY_KEY))
                with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with native Mac run"):
                    with mac_run.held(checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                        self.fail("The guest took the native run's place")

        _ = subprocess.run(["git", "-C", str(separate), "config", "--unset", "core.worktree"], check=True)
        for checkout in (separate, self.checkout):
            with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with native Mac run"):
                with mac_run.held(checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                    self.fail("A clone took the origin repository's slot")
        self.assertEqual(mac_test.command_release(native, self.paths), 0)
        config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("fixture-mac", 1, 1),
                                           1, 1, 1, "1", {"linux_only..claude": "", "linux_only.hana": ""})
        for checkout in (separate, self.checkout):
            self.assertEqual(eligibility.mac_step_eligibility(config, checkout, "hana", "nextest", "mac"),
                             eligibility.KnownRepository(".claude", REPOSITORY_KEY))
        for checkout in (linked, peer):
            self.assertEqual(eligibility.mac_step_eligibility(config, checkout, "hana", "nextest", "mac"),
                             eligibility.IneligibleMacStep("repo"))
        linked_claim = self.native_claim(linked)
        with mac_run.held(separate, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
            self.assertEqual(mac_test.command_claim(linked_claim, self.paths, watch_claim=False), 11)
        self.assertEqual(mac_test.command_claim(linked_claim, self.paths, watch_claim=False), 0)
        self.addCleanup(mac_test.command_release, linked_claim, self.paths)
        with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with native Mac run"):
            with mac_run.held(self.repo, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                self.fail("The origin repository bypassed the whole-Mac claim")

    def test_mac_origin_takes_precedence_over_borrowed_objects(self) -> None:
        other = self.root / "other"
        _ = subprocess.run(["git", "clone", "-q", "--reference", str(self.repo),
                            str(self.repo), str(other)], check=True)
        self.assertEqual(repository.borrowed_git_dir(str(other / ".git")), str(self.repo / ".git"))
        origin = "https://example.invalid/team/other.git"
        _ = subprocess.run(["git", "-C", str(other), "remote", "set-url", "origin", origin], check=True)
        _ = subprocess.run(["git", "-C", str(self.checkout), "remote", "set-url", "origin", "../other"], check=True)
        for checkout in (other, self.checkout):
            with self.subTest(checkout=checkout):
                saved = claim_state.mac_repository_identity(str(checkout))
                assert isinstance(saved, claim_state.MacMirrorIdentity)
                self.assertEqual((saved.key, saved.mirror_name), (origin.removesuffix(".git"), "other"))

    def test_separate_origins_with_one_base_name_get_separate_claim_files(self) -> None:
        for team in ("team-a", "team-b"):
            repo = self.repository(team)
            _ = subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                                f"https://example.invalid/{team}/project.git"], check=True)
            args = self.native_claim(repo)
            self.assertEqual(mac_test.command_claim(args, self.paths, watch_claim=False), 0)
            self.addCleanup(mac_test.command_release, args, self.paths)
        claims = claim_state.read_claims(self.paths)
        self.assertEqual(len({claim.path for claim in claims}), 2)
        self.assertTrue(all(claim.record.get("repo_key", "").endswith("/project") for claim in claims))
        self.assertEqual({claim.record["mac_repository"]["mirror_name"] for claim in claims
                          if "mac_repository" in claim.record}, {"team-a", "team-b"})

    def test_legacy_kept_claim_accepts_exact_recovery_after_blocking_other_work(self) -> None:
        start = process_identity.process_start(os.getpid())
        assert isinstance(start, process_identity.Started)
        path = self.paths.claims / "checkout.json"
        path.parent.mkdir(parents=True)
        with tempfile.TemporaryDirectory(dir=self.root) as folder:
            checkout = Path(folder)
            _ = subprocess.run(["git", "init", "-q", folder], check=True)
            mac_test.write_json(path, {"pid": os.getpid(), "proc_start": "ended", "what": "old check",
                "worktree": folder, "since": "2026-11-21T10:00:00+00:00",
                "claim_id": "old", "episode": "old-episode", "attempt": "old-attempt", "submitting_session": "",
                "remote_host": "fixture-mac", "repo_name": "checkout"})
        self.assertFalse(checkout.exists())
        other = self.repository("unrelated")
        waiting = self.native_claim(other)
        with mock.patch.object(process_identity, "process_start", return_value=process_identity.Unreadable("fixture")):
            self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False), 11)
        self.assertEqual(phase_wait_holder.mac_owner("mac", str(other)),
                         ("--holder-unit", "old check (kept for cleanup)"))
        self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False), 13)
        saved, = claim_state.read_claims(self.paths)
        self.assertEqual(saved.path, path)
        self.assertEqual(saved.scope, claim_state.WHOLE_MAC)
        self.assertEqual(saved.record["proc_start"], start.start)
        self.assertEqual(saved.record["claim_id"], "old")
        self.assertEqual(build_targets.wait_claim_identity(self.paths, str(other), waiting.pid, session=NoOwningSession()),
                         claim_state.claim_identity(saved.record))
        assignments = self.root / "assignment.json"
        assignment = placement.PlacementAssignment("new-episode", "new-attempt",
            placement.PlacementDestination("mac", "mac", "fixture-mac", claim_state.target_claim_scope(self.paths, str(other))),
            execution_identity.SourceKeyUnavailable("The fixture has no source key"),
            placement.SubmittingOwner("fixture-host", waiting.pid, start.start), NoOwningSession(), 0)
        _ = assignments.write_text(wire.encode_reply(wire.Assignment(assignment)))
        reservation = build_targets.CliArguments()
        reservation.pid, reservation.repo_root = waiting.pid, str(other)
        reservation.assignment_file = str(assignments)
        reservation.reservation_file = str(self.root / "reservation")
        identity_file = self.root / "reservation.identity"
        reservation.claim_identity_file = str(identity_file)
        reservation.recover_only = True
        self.assertEqual(build_targets.reserve_assigned(reservation), 13)
        self.assertEqual(identity_file.read_text(), "old\told-episode\told-attempt\t\n")
        reservation.recover_only = False
        with mock.patch.object(repository, "mac_source_repository", side_effect=AssertionError("Use saved scope")):
            self.assertEqual(build_targets.reserve_assigned(reservation), 13)
        runner = self.native_claim(other)
        runner.remote, runner.from_pid = "fixture-mac", waiting.pid
        runner.claim_id, runner.episode, runner.attempt = "old", "old-episode", "old-attempt"
        with mock.patch.object(repository, "mac_source_repository", side_effect=AssertionError("No Git on handoff")):
            self.assertEqual(mac_test.command_claim(runner, self.paths, watch_claim=False), 13)
        args = build_targets.CliArguments()
        args.pid, args.repo_root = runner.pid, str(other)
        target = target_config.SshTarget("mac", "macos", self.root / "mac_test.conf")
        connection = eligibility.SshConnectionSettings("fixture-mac", 1, 1)
        with mock.patch.object(eligibility, "read_connection_settings", return_value=connection), \
                mock.patch.object(offload, "check_leftovers", return_value=offload.LeftoversEnded()) as check:
            before = path.read_bytes()
            with copies.remote_host_lease("fixture-mac", exclusive=True):
                build_targets.cleanup_refused_claims(target, args, session=NoOwningSession())
            check.assert_not_called()
            self.assertEqual(path.read_bytes(), before)
            build_targets.cleanup_refused_claims(target, args, session=NoOwningSession())
        check.assert_called_once_with(connection, "fixture-mac", offload.ALL_MIRRORS, "mac", 1)
        self.assertEqual(claim_state.read_claims(self.paths), ())

    def test_legacy_wait_claim_retires_under_the_new_execution_lease(self) -> None:
        with (self.root / "mac_test.conf").open("a") as settings:
            _ = settings.write("mirror_root=~/.local/state/mac-test/mirror\n")
        source_b = self.repository("source-b")
        remote = ["git", "-C", str(self.checkout), "remote", "set-url", "origin"]
        self.addCleanup(step_estimator.clear_git_identity_cache)
        cases = ((True, True, False), (False, True, False), (True, False, False), (False, False, True))
        for assigned, changed, structured in cases:
            with self.subTest(assigned=assigned, changed=changed, structured=structured):
                _ = subprocess.run([*remote, str(self.repo)], check=True)
                waiting = self.native_claim(self.checkout)
                self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False), 0)
                reserved, = claim_state.read_claims(self.paths)
                record = reserved.record.copy()
                if structured:
                    legacy = reserved.path
                    assert "mac_repository" in record
                    saved_record = record["mac_repository"].copy()
                    _ = saved_record.pop("history_key")
                    record["mac_repository"] = saved_record
                else:
                    legacy = reserved.path.with_name("legacy.json")
                    _ = record.pop("mac_repository")
                    reserved.path.unlink()
                mac_test.write_json(legacy, record)
                identity = claim_state.claim_identity(record)
                assignment = placement.PlacementAssignment(identity.episode, identity.attempt,
                    placement.PlacementDestination("mac", "mac", "fixture-mac", reserved.scope),
                    execution_identity.SourceKeyUnavailable("The fixture has no source key"),
                    placement.SubmittingOwner("fixture-host", waiting.pid, record["proc_start"]), NoOwningSession(), 0)
                request = offload.OffloadRequest(self.checkout, "hana", "test", offload.NO_RESULT_FILE, (),
                    "verify", "", target="mac", placement=(offload.AssignedOffloadRequest(assignment) if assigned
                                                           else offload.DirectOffloadRequest()),
                    admission=offload.MacClaimFromWait(waiting.pid, identity, NoOwningSession()))
                config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("fixture-mac", 1, 1),
                                                   1, 1, 1, "1", {"linux_only.source-b": "", "linux_only..claude": "",
                                                                    "mirror_busy_backoff_s": "0"})
                if changed:
                    _ = subprocess.run([*remote, str(source_b)], check=True)
                step_estimator.clear_git_identity_cache()
                queue_repository, _ = step_estimator.git_identity(["cargo", "check"], str(self.checkout))
                assert isinstance(queue_repository, RepositoryIdentity)
                repository_name = "source-b" if changed else ".claude"
                mirror = offload.request_mirror(config, replace(request, slot=1), eligibility.KnownRepository(repository_name))
                before = legacy.read_bytes()
                claim_calls: list[str] = []

                launch = cast(Callable[..., object], subprocess.Popen)

                def claim_child(command: list[str], **options: object) -> object:
                    if command[0] == "git":
                        return launch(command, **options)
                    self.assertEqual(command[2], "claim")
                    claim_calls.append(command[2])
                    with self.assertRaises(copies.BusyRemoteMirror), copies.remote_lease(mirror):
                        self.fail("The runner did not hold the execution lease during the claim")
                    args = mac_test.parser().parse_args(command[2:], namespace=mac_test.CliArguments())
                    output = StringIO()
                    if "--from-pid" in command:
                        self.assertEqual(legacy.read_bytes(), before)
                        self.assertIn("--retire-wait", command)
                    with redirect_stdout(output):
                        status = mac_test.command_claim(args, self.paths, watch_claim=False)
                    self.assertIn("--mac-repository", command)
                    return mock.Mock(returncode=status, communicate=mock.Mock(return_value=(output.getvalue(), "")),
                                     poll=mock.Mock(return_value=status))

                with mock.patch.object(subprocess, "Popen", side_effect=claim_child), \
                        mock.patch.object(repository, "repo_name", side_effect=AssertionError("Do not resolve legacy policy")), \
                        mock.patch.object(offload, "claimed_run", return_value=offload.PassedOnMac(1)) as run:
                    with copies.remote_lease(mirror):
                        blocked = offload.run_offload(offload.ProcessController(), config, request, time.monotonic())
                    self.assertIsInstance(blocked, offload.BusyMacDecline)
                    self.assertEqual(legacy.read_bytes(), before)
                    self.assertEqual(claim_calls, [])
                    controller = offload.ProcessController()
                    result = offload.run_offload(controller, config, request, time.monotonic())
                if not structured:
                    self.assertFalse(legacy.exists())
                if assigned and changed:
                    assert isinstance(result, offload.DeclinedMacRun)
                    self.assertEqual(result.reason, "source_changed")
                    self.assertEqual(len(claim_calls), 1)
                    run.assert_not_called()
                    self.assertEqual(claim_state.read_claims(self.paths), ())
                else:
                    self.assertIsInstance(result, offload.PassedOnMac)
                    self.assertEqual(len(claim_calls), 1)
                    run.assert_called_once()
                    fresh, = claim_state.read_claims(self.paths)
                    self.assertNotIn("replaced_reservation", fresh.record)
                    saved = claim_state.saved_mac_repository(fresh.record)
                    assert isinstance(saved, claim_state.MacRepositoryIdentity)
                    self.assertEqual(saved.mirror_name, "source-b" if changed else ".claude")
                    self.assertEqual(saved.history_key.key, queue_repository.key)
                    self.assertEqual(controller.attempt_evidence.repository_key, queue_repository.key)
                    fresh_identity = claim_state.claim_identity(fresh.record)
                    if assigned:
                        self.assertEqual(fresh_identity.claim_id, execution_identity.assigned_claim_id(
                            assignment.episode, assignment.attempt))
                    else:
                        self.assertNotEqual(fresh_identity, identity)
                        self.assertEqual(fresh_identity.episode, request.call_id)
                    self.assertEqual(run.call_args.args[3], eligibility.KnownRepository(saved.mirror_name, saved.key))
                    waiting.claim_id, waiting.episode, waiting.attempt = (fresh_identity.claim_id, fresh_identity.episode,
                                                                          fresh_identity.attempt)
                    self.assertEqual(mac_test.command_release(waiting, self.paths), 0)

    def test_guest_waits_for_the_replacement_and_cannot_take_its_slot(self) -> None:
        waiting = self.native_claim(self.checkout)
        self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False), 0)
        reserved, = claim_state.read_claims(self.paths)
        legacy = reserved.path.rename(reserved.path.with_name("legacy.json"))
        incomplete = reserved.record.copy()
        _ = incomplete.pop("mac_repository")
        mac_test.write_json(legacy, incomplete)
        resolved = claim_state.mac_repository_identity(str(self.checkout))
        assert isinstance(resolved, claim_state.ResolvedMacMirror)
        saved = resolved.claim_repository()
        assert isinstance(saved, claim_state.MacRepositoryIdentity)
        runner = self.native_claim(self.checkout)
        runner.what, runner.remote, runner.from_pid = "replacement", "fixture-mac", waiting.pid
        runner.retire_wait = "replacement-episode"
        runner.mac_repository = json.dumps(saved.record())
        retiring = Event()
        guest_waits = Event()
        lock = claim_state.locked

        @contextmanager
        def guest_lock(path: Path) -> Generator[None]:
            with path.open("a+") as lock_file:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                guest_waits.set()
            with lock(path):
                yield

        def claim_guest() -> None:
            self.assertTrue(retiring.wait(3))
            with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with replacement"):
                with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                    self.fail("The guest took the shared slot")

        def retire(path: Path, missing_ok: bool = False) -> None:
            if path == legacy:
                retiring.set()
                self.assertTrue(guest_waits.wait(3))
            unlink(path, missing_ok=missing_ok)

        unlink = Path.unlink
        with ThreadPoolExecutor(max_workers=1) as guests, \
                mock.patch.object(claim_state, "locked", new=guest_lock), \
                mock.patch.object(Path, "unlink", autospec=True, side_effect=retire):
            guest = guests.submit(claim_guest)
            status = mac_test.command_claim(runner, self.paths, watch_claim=False)
            guest.result(timeout=3)
        self.assertEqual(status, 0)
        fresh, = claim_state.read_claims(self.paths)
        self.assertEqual(fresh.path, reserved.path)
        self.assertEqual(fresh.record["what"], "replacement")
        runner.claim_id, runner.episode, runner.attempt = fresh.record["claim_id"], fresh.record["episode"], fresh.record["attempt"]
        self.assertEqual(mac_test.command_release(runner, self.paths), 0)

    def test_interrupted_replacement_retires_both_records_and_retries(self) -> None:
        self.exercise_interrupted_replacement(("retry", "inspect"))

    def test_lost_replacement_reply_recovers_the_completed_claim(self) -> None:
        self.exercise_interrupted_replacement(("lost-reply",))

    def test_retry_cleans_a_committed_replacement_after_the_owner_dies(self) -> None:
        self.exercise_interrupted_replacement(("committed",))

    def test_status_and_sweep_retire_an_interrupted_replacement(self) -> None:
        self.exercise_interrupted_replacement(("status", "sweep"))

    def test_release_wait_revokes_an_interrupted_replacement_retry(self) -> None:
        self.exercise_interrupted_replacement(("release-wait",))

    def test_release_wait_revokes_a_retry_with_an_unreadable_process_start(self) -> None:
        self.exercise_interrupted_replacement(("release-unreadable", "release-before-unlink"))

    def test_release_wait_prevents_retry_after_a_stop_before_permission(self) -> None:
        self.exercise_interrupted_replacement(("release-before-permission",))

    def test_interrupted_release_wait_cannot_restore_retry_permission(self) -> None:
        self.exercise_interrupted_replacement(("release-before-proof-removal", "release-before-claim-removal"))

    def test_release_wait_reports_its_damaged_reservation_without_a_proof(self) -> None:
        self.exercise_interrupted_replacement(("release-damaged-before-permission",))

    def add_linux_target(self) -> None:
        targets = self.root / "build_targets.conf"
        _ = targets.write_text(targets.read_text().replace("targets=natedev,mac", "targets=natedev,mac,linux")
                               + "kind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux.conf\n")
        _ = (self.root / "linux.conf").write_text("offload=on\nslots.claude_commands=1\n")

    def test_release_wait_reports_damage_on_mac_and_linux(self) -> None:
        self.add_linux_target()
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(self.root / "target-state")}):
            for target in ("mac", "linux"):
                with self.subTest(target=target):
                    paths = claim_state.state_paths(target)
                    waiter = self.native_claim(self.checkout)
                    waiter.target = target
                    waiter.claim_id = target + "-waiter"
                    self.assertEqual(mac_test.command_claim(waiter, paths, watch_claim=False), 0)
                    claim, = claim_state.read_claims(paths)
                    original = claim.path.read_bytes()
                    damaged = (json.dumps({**claim.record, "remote_host": None}).encode()
                               if target == "mac" else b"{")
                    _ = claim.path.write_bytes(damaged)
                    identity = claim_state.claim_identity(claim.record)
                    with redirect_stderr(StringIO()) as diagnostic:
                        self.assertEqual(build_targets.command_release_wait(waiter.pid, identity=identity), 1)
                    self.assertIn(str(claim.path), diagnostic.getvalue())
                    self.assertEqual(claim.path.read_bytes(), damaged)
                    _ = claim.path.write_bytes(original)
                    self.assertEqual(build_targets.command_release_wait(waiter.pid, identity=identity), 0)
                    self.assertFalse(claim.path.exists())
                    if target == "mac":
                        _ = paths.run.write_bytes(b"{")

    def test_release_wait_names_the_invalid_mac_repository_file(self) -> None:
        waiter = self.native_claim(self.checkout)
        self.assertEqual(mac_test.command_claim(waiter, self.paths, watch_claim=False), 0)
        claim, = claim_state.read_claims(self.paths)
        damaged = json.dumps({**claim.record, "mac_repository": None}).encode()
        _ = claim.path.write_bytes(damaged)
        identity = claim_state.claim_identity(claim.record)
        with redirect_stderr(StringIO()) as diagnostic:
            self.assertEqual(build_targets.command_release_wait(waiter.pid, identity=identity), 1)
        self.assertIn(f"{claim.path}: invalid Mac repository identity", diagnostic.getvalue())
        self.assertEqual(claim.path.read_bytes(), damaged)

    def test_repeated_release_wait_skips_a_reused_slot_on_mac_and_linux(self) -> None:
        self.add_linux_target()
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(self.root / "target-state")}):
            for target in ("mac", "linux"):
                with self.subTest(target=target):
                    paths = claim_state.state_paths(target)
                    waiter = self.native_claim(self.checkout)
                    waiter.target, waiter.claim_id = target, f"{target}-first"
                    self.assertEqual(mac_test.command_claim(waiter, paths, watch_claim=False), 0)
                    first, = claim_state.read_claims(paths)
                    identity = claim_state.claim_identity(first.record)
                    pending = claim_state.claim_pending_start(paths, first.path, first.record)
                    if target == "linux":
                        pending.parent.mkdir(parents=True, exist_ok=True)
                        pending.touch()
                    self.assertEqual(build_targets.command_release_wait(waiter.pid, identity=identity), 0)
                    self.assertFalse(first.path.exists())
                    self.assertFalse(pending.exists())
                    later = self.native_claim(self.checkout)
                    later.target, later.claim_id = target, f"{target}-later"
                    self.assertEqual(mac_test.command_claim(later, paths, watch_claim=False), 0)
                    second, = claim_state.read_claims(paths)
                    self.assertEqual(second.path, first.path)
                    original = second.path.read_bytes()
                    _ = second.path.write_bytes(b"{")
                    with redirect_stderr(StringIO()) as diagnostic:
                        self.assertEqual(build_targets.command_release_wait(waiter.pid, identity=identity), 0)
                    self.assertEqual(diagnostic.getvalue(), "")
                    self.assertEqual(second.path.read_bytes(), b"{")
                    _ = second.path.write_bytes(original)
                    self.assertEqual(mac_test.command_release(later, paths), 0)

    def test_damaged_wait_removal_is_refused_and_swept_when_its_slot_is_free(self) -> None:
        waiter = self.native_claim(self.checkout)
        self.assertEqual(mac_test.command_claim(waiter, self.paths, watch_claim=False), 0)
        original, = claim_state.read_claims(self.paths)
        identity = claim_state.claim_identity(original.record)
        removal = claim_state.released_wait_path(self.paths, identity)
        self.assertEqual(build_targets.command_release_wait(waiter.pid, identity=identity), 0)
        self.assertTrue(removal.is_file())

        successor = self.native_claim(self.checkout)
        successor.claim_id = "successor"
        self.assertEqual(mac_test.command_claim(successor, self.paths, watch_claim=False), 0)
        reused, = claim_state.read_claims(self.paths)
        self.assertEqual(reused.path, original.path)
        other = self.native_claim(self.repository("hana", origin=HANA_ORIGIN))
        self.assertEqual(mac_test.command_claim(other, self.paths, watch_claim=False), 0)

        for damaged in ("{", json.dumps({**original.record, "claim_id": "wrong"})):
            with self.subTest(damaged=damaged):
                _ = removal.write_text(damaged)
                with redirect_stderr(StringIO()) as diagnostic:
                    self.assertEqual(build_targets.command_release_wait(waiter.pid, identity=identity), 1)
                self.assertIn(str(removal), diagnostic.getvalue())
                with redirect_stderr(StringIO()) as diagnostic, claim_state.locked(self.paths.lock):
                    self.assertIn(reused, mac_test.live_claims(self.paths))
                self.assertEqual(diagnostic.getvalue(), "")
                self.assertEqual(removal.read_text(), damaged)

        self.assertEqual(mac_test.command_release(successor, self.paths), 0)
        with redirect_stderr(StringIO()) as diagnostic, claim_state.locked(self.paths.lock):
            for _ in range(2):
                remaining, = mac_test.live_claims(self.paths)
                self.assertEqual(remaining.record["claim_id"], other.claim_id)
        self.assertIn(str(removal), diagnostic.getvalue())
        self.assertEqual(len(diagnostic.getvalue().splitlines()), 1)
        self.assertFalse(removal.exists())

    def test_wait_removal_read_failure_reports_once_and_keeps_the_record(self) -> None:
        waiter = self.native_claim(self.checkout)
        self.assertEqual(mac_test.command_claim(waiter, self.paths, watch_claim=False), 0)
        claim, = claim_state.read_claims(self.paths)
        identity = claim_state.claim_identity(claim.record)
        self.assertEqual(build_targets.command_release_wait(waiter.pid, identity=identity), 0)
        removal = claim_state.released_wait_path(self.paths, identity)
        contents = removal.read_bytes()
        read_text = Path.read_text

        for failure in (PermissionError(errno.EACCES, "fixture permission error"), OSError(errno.EIO, "fixture I/O error")):
            with self.subTest(failure=failure):
                def read_with_failure(path: Path, *, encoding: str = "utf-8") -> str:
                    if path == removal:
                        raise failure
                    return read_text(path, encoding=encoding)

                with mock.patch.object(Path, "read_text", autospec=True, side_effect=read_with_failure):
                    with redirect_stderr(StringIO()) as diagnostic, claim_state.locked(self.paths.lock):
                        self.assertEqual(mac_test.live_claims(self.paths), ())
                        self.assertEqual(mac_test.live_claims(self.paths), ())
                    self.assertEqual(diagnostic.getvalue().splitlines(), [f"{removal}: {failure}"])
                    self.assertEqual(removal.read_bytes(), contents)
                    with redirect_stderr(StringIO()) as diagnostic:
                        self.assertEqual(build_targets.command_release_wait(waiter.pid, identity=identity), 1)
                    self.assertEqual(diagnostic.getvalue().splitlines(), [f"{removal}: {failure}"])
                with claim_state.locked(self.paths.lock):
                    self.assertEqual(mac_test.live_claims(self.paths), ())
                self.assertEqual(removal.read_bytes(), contents)

        self.assertEqual(build_targets.command_release_wait(waiter.pid, identity=identity), 0)

    def test_repeated_release_wait_skips_unrelated_damage(self) -> None:
        waiter = self.native_claim(self.checkout)
        self.assertEqual(mac_test.command_claim(waiter, self.paths, watch_claim=False), 0)
        claim, = claim_state.read_claims(self.paths)
        self.assertNotEqual(claim.path, self.paths.run)
        _ = self.paths.run.write_bytes(b"{")
        identity = claim_state.claim_identity(claim.record)
        with redirect_stderr(StringIO()) as diagnostic:
            for _ in range(2):
                self.assertEqual(build_targets.command_release_wait(waiter.pid, identity=identity), 0)
        self.assertEqual(diagnostic.getvalue(), "")
        self.assertFalse(claim.path.exists())
        self.assertEqual(self.paths.run.read_bytes(), b"{")

    def test_release_wait_revokes_a_retry_with_an_unrelated_damaged_claim(self) -> None:
        self.exercise_interrupted_replacement(("release-with-damaged-claim",))

    def test_release_wait_uses_the_proof_before_a_damaged_location(self) -> None:
        self.exercise_interrupted_replacement(("release-with-damaged-location",))

    def test_sweep_removes_a_retry_proof_when_the_waiter_ends(self) -> None:
        self.exercise_interrupted_replacement(("waiter-ended",))

    def test_a_second_interruption_allows_a_fresh_retry(self) -> None:
        self.exercise_interrupted_replacement(("second-interruption",))

    def test_a_retry_uses_a_new_fence_after_settlement(self) -> None:
        self.exercise_interrupted_replacement(("settled-fence",))

    def test_status_keeps_an_older_replacement_with_a_pending_start(self) -> None:
        self.exercise_interrupted_replacement(("legacy-pending",))

    def test_status_keeps_an_older_replacement_after_the_start_settles(self) -> None:
        self.exercise_interrupted_replacement(("legacy-launched",))

    def test_failed_retirement_keeps_the_reservation_and_allows_an_unrelated_claim(self) -> None:
        self.exercise_interrupted_replacement(("unlink-error",))

    def exercise_interrupted_replacement(self, recoveries: tuple[str, ...]) -> None:
        script = """
import os
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
from unittest.mock import patch

from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_targets import build_targets
from scripts.mac_test import claim_state, mac_test

checkpoint = sys.argv[1]
old = Path(sys.argv[2])
args = mac_test.parser().parse_args(sys.argv[3:], namespace=mac_test.CliArguments())
if checkpoint in ("committed", "legacy-launched"):
    args.pid = os.getpid()
paths = mac_test.state_paths("mac")
unlink = Path.unlink
replace = Path.replace
write_json = mac_test.write_json

def interrupt_unlink(self, *args, **kwargs):
    if checkpoint in ("retire", "legacy-launched") and self == old:
        os._exit(71)
    if checkpoint == "release-before-proof-removal" and self.parent == paths.retired_reservations:
        os._exit(75)
    return unlink(self, *args, **kwargs)

def interrupt_replace(self, *args, **kwargs):
    if checkpoint == "release-before-claim-removal" and self == old:
        os._exit(75)
    return replace(self, *args, **kwargs)

def interrupt_write(path, record):
    if checkpoint == "fresh-write" and "remote_host" in record:
        assert not claim_state.read_claims(paths)
        os._exit(72)
    write_json(path, record)
    if checkpoint == "release-before-permission" and "replaced_reservation" in record:
        os._exit(74)
    if checkpoint == "committed" and "remote_host" in record and "replaced_reservation" not in record:
        os._exit(73)

with patch.object(Path, "unlink", interrupt_unlink), patch.object(Path, "replace", interrupt_replace), \
        patch.object(mac_test, "write_json", interrupt_write):
    if checkpoint in ("release-before-proof-removal", "release-before-claim-removal"):
        identity = claim_state.ClaimIdentity(args.claim_id, args.episode, args.attempt)
        sys.exit(build_targets.command_release_wait(args.from_pid, identity=identity, session=NoOwningSession()))
    if checkpoint == "lost-reply":
        with redirect_stdout(StringIO()):
            sys.exit(mac_test.command_claim(args, paths, watch_claim=False, session=args.session))
    sys.exit(mac_test.command_claim(args, paths, watch_claim=False, session=args.session))
"""
        resolved = claim_state.mac_repository_identity(str(self.checkout))
        assert isinstance(resolved, claim_state.ResolvedMacMirror)
        saved = resolved.claim_repository()
        assert isinstance(saved, claim_state.MacRepositoryIdentity)
        with (self.root / "mac_test.conf").open("a") as settings:
            _ = settings.write("mirror_root=~/.local/state/mac-test/mirror\n")
        config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("fixture-mac", 1, 1),
                                           1, 1, 1, "1", {"linux_only.checkout": ""})
        for recovery in recoveries:
            with self.subTest(recovery=recovery):
                waiting = self.native_claim(self.checkout)
                if recovery in ("committed", "legacy-launched"):
                    waiting.episode, waiting.attempt = "episode", "attempt"
                    waiting.claim_id = execution_identity.assigned_claim_id(waiting.episode, waiting.attempt)
                reservation = saved if recovery == "unlink-error" else claim_state.WHOLE_MAC
                self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False,
                                                       mac_repository=reservation), 0)
                reserved, = claim_state.read_claims(self.paths)
                old_path = reserved.path
                if recovery == "unlink-error":
                    old_path = old_path.rename(self.paths.claims / "legacy.json")
                    incomplete = reserved.record.copy()
                    del incomplete["mac_repository"]
                    mac_test.write_json(old_path, incomplete)
                identity = claim_state.claim_identity(reserved.record)
                scope = claim_state.RepositoryScope(saved.mirror_name, saved.key)
                assignment = placement.PlacementAssignment("episode", "attempt",
                    placement.PlacementDestination("mac", "mac", "fixture-mac", scope),
                    execution_identity.SourceKeyUnavailable("The fixture has no source key"),
                    placement.SubmittingOwner("fixture-host", waiting.pid, reserved.record["proc_start"]),
                    NoOwningSession(), 0)
                route = (offload.DirectOffloadRequest() if recovery in ("status", "second-interruption")
                         else offload.AssignedOffloadRequest(assignment))
                request = offload.OffloadRequest(self.checkout, "hana", "test", offload.NO_RESULT_FILE, (),
                    "verify", "", target="mac", placement=route,
                    admission=offload.MacClaimFromWait(waiting.pid, identity, NoOwningSession()))
                command = offload.claim_arguments(request, config, retire_wait=True, mac_repository=saved)
                before_permission = recovery in ("release-before-permission", "release-damaged-before-permission")
                checkpoint = ("release-before-permission" if before_permission
                              else recovery if recovery in ("committed", "legacy-launched") else "retire")
                child = subprocess.run([sys.executable, "-c", script, checkpoint, str(old_path), *command[2:]],
                                       capture_output=True, text=True, timeout=5)
                if recovery == "committed":
                    self.assertEqual(child.returncode, 73, child.stdout + child.stderr)
                    self.assertFalse(old_path.exists())
                    completed, = claim_state.read_claims(self.paths)
                    self.assertNotIn("replaced_reservation", completed.record)
                    self.assertEqual(claim_state.claim_identity(completed.record), identity)
                    self.assertNotEqual(completed.record["pid"], waiting.pid)
                    self.assertTrue(mac_test.recoverable_kept_claim(completed.record))
                    self.check_committed_replacement_retry(request, assignment, config, completed, script)
                    continue
                expected_exit = 74 if before_permission else 71
                self.assertEqual(child.returncode, expected_exit, child.stdout + child.stderr)
                self.assertTrue(old_path.exists())
                abandoned, = (claim for claim in claim_state.read_claims(self.paths) if claim.path != old_path)
                self.assertIn("replaced_reservation", abandoned.record)
                if before_permission:
                    proof = claim_state.retired_mac_reservation_path(self.paths, identity)
                    self.assertFalse(proof.exists())
                    self.assertFalse(claim_state.claim_pending_start(self.paths, abandoned.path, abandoned.record).exists())
                    unreadable = process_identity.Unreadable("fixture")
                    with mock.patch.object(process_identity, "process_start", return_value=unreadable):
                        if recovery == "release-damaged-before-permission":
                            original = old_path.read_bytes()
                            for damage in (b"{", b"{}"):
                                with self.subTest(damage=damage), redirect_stderr(StringIO()) as diagnostic:
                                    _ = old_path.write_bytes(damage)
                                    self.assertEqual(build_targets.command_release_wait(waiting.pid, identity=identity), 1)
                                    self.assertIn(str(old_path), diagnostic.getvalue())
                                    self.assertEqual(old_path.read_bytes(), damage)
                                    self.assertFalse(proof.exists())
                            _ = old_path.write_bytes(original)
                        with mock.patch.object(claim_state, "submitting_session", return_value=OwningSession("other")):
                            self.assertEqual(build_targets.command_release_wait(waiting.pid, identity=identity), 0)
                        self.assertEqual(claim_state.read_run(old_path), reserved.record)
                        self.assertEqual(build_targets.command_release_wait(waiting.pid, identity=identity), 0)
                    self.assertEqual(process_identity.process_start(waiting.pid),
                                     process_identity.Started(reserved.record["proc_start"]))
                    with claim_state.locked(self.paths.lock):
                        self.assertEqual(mac_test.live_claims(self.paths), ())
                    self.assertFalse(proof.exists())
                    child = subprocess.run([sys.executable, "-c", script, "none", str(old_path), *command[2:]],
                                           capture_output=True, text=True, timeout=5)
                    self.assertEqual(child.returncode, 12, child.stdout + child.stderr)
                    self.assertIn("no reservation", child.stderr)
                    self.assertEqual(claim_state.read_claims(self.paths), ())
                    continue
                if recovery in ("release-before-proof-removal", "release-before-claim-removal"):
                    proof = claim_state.retired_mac_reservation_path(self.paths, identity)
                    self.assertTrue(proof.exists())
                    child = subprocess.run([sys.executable, "-c", script, recovery, str(old_path), *command[2:]],
                                           capture_output=True, text=True, timeout=5)
                    self.assertEqual(child.returncode, 75, child.stdout + child.stderr)
                    self.assertTrue(old_path.exists())
                    self.assertEqual(proof.exists(), recovery == "release-before-proof-removal")
                    child = subprocess.run([sys.executable, "-c", script, "none", str(old_path), *command[2:]],
                                           capture_output=True, text=True, timeout=5)
                    self.assertEqual(child.returncode, 12, child.stdout + child.stderr)
                    self.assertIn("no reservation", child.stderr)
                    self.assertFalse(proof.exists())
                    self.assertEqual(claim_state.read_claims(self.paths), ())
                    continue
                if recovery == "unlink-error":
                    self.check_unrelated_acquisition_after_failed_retirement(old_path)
                    continue
                if recovery in ("legacy-pending", "legacy-launched"):
                    pending = recovery == "legacy-pending"
                    self.check_legacy_replacement(reserved, abandoned, pending=pending)
                    if not pending:
                        kept, = claim_state.read_claims(self.paths)
                        self.assertTrue(mac_test.recoverable_kept_claim(kept.record))
                        self.check_committed_replacement_retry(request, assignment, config, kept, script)
                    continue
                if recovery == "status":
                    status_config = mac_test.MacTestConfig("", "", "", "", 1, 0, 1, 1, 1)
                    self.assertEqual(mac_test.command_status(self.paths, status_config), 0)
                elif recovery in ("sweep", "release-wait", "release-unreadable", "waiter-ended"):
                    with claim_state.locked(self.paths.lock):
                        self.assertEqual(mac_test.live_claims(self.paths), ())
                elif recovery == "inspect":
                    inspector = offload.ProcessController()
                    inspector.placement = request.placement
                    inspection = offload.LocalClaimAttempt()
                    inspector.inspect_claim(inspection, claim_state.claim_identity(abandoned.record))
                    self.assertIsInstance(inspection.answer, offload.MacClaimNotConfirmed)
                    self.assertEqual(inspector.claim_identity, claim_state.NO_CLAIM_IDENTITY)
                elif recovery == "second-interruption":
                    child = subprocess.run([sys.executable, "-c", script, "fresh-write", str(old_path), *command[2:]],
                                           capture_output=True, text=True, timeout=5)
                    self.assertEqual(child.returncode, 72, child.stdout + child.stderr)
                if recovery in ("status", "sweep", "release-wait", "release-unreadable",
                                "waiter-ended", "inspect", "second-interruption"):
                    self.assertEqual(claim_state.read_claims(self.paths), ())
                if recovery == "waiter-ended":
                    proof = claim_state.retired_mac_reservation_path(self.paths, identity)
                    self.assertTrue(proof.exists())
                    for start in (process_identity.Unreadable("fixture"), process_identity.Gone()):
                        with claim_state.locked(self.paths.lock), \
                                mock.patch.object(process_identity, "process_start", return_value=start):
                            self.assertEqual(mac_test.live_claims(self.paths), ())
                        self.assertEqual(proof.exists(), isinstance(start, process_identity.Unreadable))
                    continue
                if recovery in ("release-wait", "release-unreadable", "release-before-unlink",
                                "release-with-damaged-claim", "release-with-damaged-location"):
                    proof = claim_state.retired_mac_reservation_path(self.paths, identity)
                    self.assertTrue(proof.exists())
                    self.assertEqual(old_path.exists(), recovery not in ("release-wait", "release-unreadable"))
                    if recovery == "release-unreadable":
                        saved_proof = cast(dict[str, object], json.loads(proof.read_text()))
                        _ = saved_proof.pop("reservation_file")
                        _ = proof.write_text(json.dumps(saved_proof))
                    with mock.patch.object(claim_state, "submitting_session", return_value=OwningSession("other")):
                        self.assertEqual(build_targets.command_release_wait(waiting.pid, identity=identity), 0)
                    self.assertTrue(proof.exists())
                    start = (process_identity.Started(reserved.record["proc_start"]) if recovery == "release-wait"
                             else process_identity.Unreadable("fixture"))
                    damaged = self.paths.claims / "unrelated.json"
                    location = self.paths.wait_locations / claim_state.pending_start_key(identity)
                    if recovery == "release-with-damaged-claim":
                        _ = damaged.write_text("{")
                        location.unlink()
                    if recovery == "release-with-damaged-location":
                        _ = location.write_text("{")
                    with (mock.patch.object(process_identity, "process_start", return_value=start),
                          mock.patch.object(claim_state, "read_json_object", wraps=claim_state.read_json_object) as reads):
                        self.assertEqual(build_targets.command_release_wait(waiting.pid, identity=identity), 0)
                    self.assertIsInstance(claim_state.read_retired_mac_reservation(self.paths, identity),
                                          claim_state.NoSavedRetryPermission)
                    if recovery in ("release-with-damaged-claim", "release-with-damaged-location"):
                        self.assertEqual(reads.call_args_list, [mock.call(proof), mock.call(old_path)])
                        self.assertEqual(build_targets.command_release_wait(waiting.pid, identity=identity), 0)
                    if recovery == "release-with-damaged-claim":
                        self.assertEqual(damaged.read_text(), "{")
                        damaged.unlink()
                    child = subprocess.run([sys.executable, "-c", script, "none", str(old_path), *command[2:]],
                                           capture_output=True, text=True, timeout=5)
                    self.assertEqual(child.returncode, 12, child.stdout + child.stderr)
                    self.assertIn("no reservation", child.stderr)
                    self.assertFalse(proof.exists())
                    self.assertEqual(claim_state.read_claims(self.paths), ())
                    continue
                self.check_replacement_retry(request, config, old_path, abandoned, script,
                                             settle=recovery == "settled-fence", lose_reply=recovery == "lost-reply")

    def check_committed_replacement_retry(self, request: offload.OffloadRequest,
                                          assignment: placement.PlacementAssignment,
                                          config: eligibility.OffloadConfig, completed: claim_state.HeldClaim,
                                          script: str) -> None:
        original_arguments = offload.claim_arguments
        handoffs: list[bool] = []

        def private_claim(*args: object, **kwargs: object) -> list[str]:
            command = cast(Callable[..., list[str]], original_arguments)(*args, **kwargs)
            handoffs.append("--from-pid" in command)
            return [sys.executable, "-c", script, "none", str(completed.path), *command[2:]]

        def finish_cleanup(*_args: object) -> offload.LeftoversEnded:
            retained, = claim_state.read_claims(self.paths)
            self.assertEqual(retained.path, completed.path)
            self.assertTrue(retained.record.get("cleanup_pending"))
            return offload.LeftoversEnded()

        with (
            mock.patch.object(offload, "claim_arguments", side_effect=private_claim),
            mock.patch.object(offload, "check_current_leftovers", side_effect=finish_cleanup) as cleanup,
            mock.patch.object(offload, "claimed_run", return_value=offload.PassedOnMac(1)) as launched,
        ):
            result = offload.run_offload(offload.ProcessController(), config, request, time.monotonic())
            self.assertIsInstance(result, offload.BusyMacDecline)
            self.assertFalse(completed.path.exists())
            cleanup.assert_called_once()
            launched.assert_not_called()
            self.assertEqual(handoffs, [True])
            fresh = offload.AssignedOffloadRequest(replace(assignment, attempt="fresh-attempt"))
            result = offload.run_offload(offload.ProcessController(), config,
                                         replace(request, placement=fresh, admission=offload.UnclaimedMacRun()),
                                         time.monotonic())
            self.assertIsInstance(result, offload.PassedOnMac)
            launched.assert_called_once()
            self.assertEqual(handoffs, [True, False])
        running, = claim_state.read_claims(self.paths)
        self.assertEqual(running.record["attempt"], "fresh-attempt")
        self.assertNotEqual(claim_state.claim_fence_scope_key(running.path, running.record),
                            claim_state.claim_fence_scope_key(completed.path, completed.record))

    def check_legacy_replacement(self, reserved: claim_state.HeldClaim,
                                 replacement: claim_state.HeldClaim, *, pending: bool) -> None:
        old = reserved.record
        record = dict(replacement.record)
        saved = claim_state.saved_mac_repository(replacement.record)
        assert isinstance(saved, claim_state.MacRepositoryIdentity)
        scope = saved.claim_file.removesuffix(".json")
        record["mac_repository"] = replace(saved, fence_generation="").record()
        record["replaced_reservation"] = {
            **{key: old[key] for key in ("claim_id", "episode", "attempt", "pid", "proc_start", "since")},
            "file": str(reserved.path.relative_to(self.paths.directory)),
        }
        mac_test.write_json(replacement.path, record)
        identity = claim_state.claim_identity(replacement.record)
        marker = offload.pending_start_path(self.paths.target, identity, scope_key=scope)
        marker.parent.mkdir()
        _ = marker.write_text(offload.pending_start_key(identity))
        if not pending:
            marker.unlink()
        status_config = mac_test.MacTestConfig("", "", "", "", 1, 0, 1, 1, 1)
        self.assertEqual(mac_test.command_status(self.paths, status_config), 0)
        self.assertFalse(reserved.path.exists())
        kept, = claim_state.read_claims(self.paths)
        self.assertEqual(kept.path, replacement.path)
        self.assertNotIn("replaced_reservation", kept.record)
        self.assertEqual(claim_state.claim_identity(kept.record), identity)
        self.assertEqual(marker.exists(), pending)

    def check_replacement_retry(self, request: offload.OffloadRequest, config: eligibility.OffloadConfig,
                                old_path: Path, abandoned: claim_state.HeldClaim, script: str, *,
                                settle: bool, lose_reply: bool) -> None:
        old_identity = claim_state.claim_identity(abandoned.record)
        old_scope = claim_state.claim_fence_scope_key(abandoned.path, abandoned.record)
        old_fence = self.root / offload.start_fence_relative(old_identity, "mac", scope_key=old_scope).with_suffix(".lock")
        if settle:
            old_fence.parent.mkdir(parents=True)
            old_fence.touch()
            old_fence.unlink()

        controller = offload.ProcessController()
        original_arguments = offload.claim_arguments

        def private_claim(*args: object, **kwargs: object) -> list[str]:
            make_arguments = cast(Callable[..., list[str]], original_arguments)
            command = make_arguments(*args, **kwargs)
            return [sys.executable, "-c", script, "lost-reply" if lose_reply else "none", str(old_path), *command[2:]]

        with (
            mock.patch.object(offload, "claim_arguments", side_effect=private_claim),
            mock.patch.object(offload, "check_current_leftovers") as cleanup,
            mock.patch.object(offload, "claimed_run", return_value=offload.PassedOnMac(1)) as launched,
        ):
            result = offload.run_offload(controller, config, request, time.monotonic())
        self.assertIsInstance(result, offload.PassedOnMac)
        cleanup.assert_not_called()
        launched.assert_called_once()
        fresh, = claim_state.read_claims(self.paths)
        self.assertFalse(old_path.exists())
        self.assertNotIn("replaced_reservation", fresh.record)
        self.assertFalse(fresh.record.get("cleanup_pending"))
        self.assertEqual(fresh.record["pid"], os.getpid())
        self.assertEqual(fresh.path, abandoned.path)
        identity = claim_state.claim_identity(fresh.record)
        self.assertEqual(controller.claim_identity, identity)
        if isinstance(request.placement, offload.AssignedOffloadRequest):
            self.assertEqual(identity, old_identity)
        else:
            self.assertNotEqual(identity, old_identity)
        new_scope = claim_state.claim_fence_scope_key(fresh.path, fresh.record)
        self.assertNotEqual(new_scope, old_scope)
        self.assertEqual(controller.claim_scope_key, new_scope)
        new_fence = self.root / offload.start_fence_relative(identity, "mac", scope_key=new_scope).with_suffix(".lock")
        self.assertNotEqual(old_fence, new_fence)
        if settle:
            self.assertEqual(old_fence.parent, new_fence.parent)
            new_fence.touch()
            self.assertFalse(old_fence.exists())
        args = mac_test.CliArguments()
        args.pid = os.getpid()
        args.claim_id, args.episode, args.attempt = identity.claim_id, identity.episode, identity.attempt
        self.assertEqual(mac_test.command_release(args, self.paths), 0)
        with mock.patch.object(offload, "claim_arguments", side_effect=private_claim), \
                mock.patch.object(offload, "claimed_run") as launched_again:
            refused = offload.run_offload(offload.ProcessController(), config, request, time.monotonic())
        assert isinstance(refused, offload.WaitClaimAdoptionDeclined)
        self.assertEqual(refused.diagnostic, "state: claim child exited 12: cannot adopt the Mac claim: no reservation")
        self.assertEqual(claim_state.read_claims(self.paths), ())
        launched_again.assert_not_called()

    def check_unrelated_acquisition_after_failed_retirement(self, old_path: Path) -> None:
        before = old_path.read_bytes()
        unrelated = self.native_claim(self.repository("unrelated", origin=HANA_ORIGIN))
        real_unlink = Path.unlink

        def fail_retirement(path: Path, missing_ok: bool = False) -> None:
            if path == old_path:
                raise PermissionError("fixture cannot remove the reservation")
            real_unlink(path, missing_ok=missing_ok)

        diagnostic = StringIO()
        with mock.patch.object(Path, "unlink", autospec=True, side_effect=fail_retirement), redirect_stderr(diagnostic):
            self.assertEqual(mac_test.command_claim(unrelated, self.paths, watch_claim=False), 0)
        self.assertIn(str(old_path), diagnostic.getvalue())
        self.assertEqual(old_path.read_bytes(), before)
        self.assertEqual(len(claim_state.read_claims(self.paths)), 3)

    def test_cleanup_handoff_holds_the_mirror_lease_and_retries_direct_claim(self) -> None:
        with (self.root / "mac_test.conf").open("a") as settings:
            _ = settings.write("mirror_root=~/.local/state/mac-test/mirror\n")
        config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("fixture-mac", 1, 1),
                                           1, 1, 1, "1", {"linux_only.checkout": ""})
        waiting = self.native_claim(self.checkout)
        self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False), 0)
        held, = claim_state.read_claims(self.paths)
        assert "remote_host" not in held.record
        saved = claim_state.saved_mac_repository(held.record)
        assert isinstance(saved, claim_state.MacRepositoryIdentity)
        identity = claim_state.claim_identity(held.record)
        request = offload.OffloadRequest(self.checkout, "hana", "test", offload.NO_RESULT_FILE, (),
            "direct", "", target="mac", admission=offload.MacClaimFromWait(waiting.pid, identity, NoOwningSession()))
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                record: claim_state.RemoteJobClaim = {**held.record, "remote_host": "old-mac", "cleanup_pending": True}
                scope = claim_state.NO_SAVED_MAC_REPOSITORY if legacy else saved
                if legacy:
                    _ = record.pop("mac_repository")
                    record["repo_name"] = "old-scope"
                mac_test.write_json(held.path, record)
                controller = offload.ProcessController()
                repo = eligibility.KnownRepository(saved.mirror_name, saved.key)
                live_mirror = offload.request_mirror(config, request, repo)
                old_repo = eligibility.KnownRepository("old-mirror") if legacy else repo
                old_mirror = replace(offload.request_mirror(config, request, old_repo), host="old-mac")

                def claim(current: offload.ProcessController, _config: eligibility.OffloadConfig,
                          step: offload.OffloadRequest, _repo: eligibility.RepositoryIdentity,
                          _slots: tuple[int, ...] = (), **_options: object) -> offload.MacClaimAnswer:
                    if isinstance(step.admission, offload.MacClaimFromWait):
                        with self.assertRaises(copies.BusyRemoteMirror), copies.remote_lease(old_mirror):
                            self.fail("The cleanup lease must be held during handoff")
                        current.mac_repository = scope
                        current.claim_identity = identity
                        return offload.TakenOverMacClaim("old-mac", 1)
                    self.assertFalse(held.path.exists())
                    self.assertIsInstance(current.claim_identity, claim_state.NoClaimIdentity)
                    current.mac_repository = saved
                    with copies.remote_lease(old_mirror):
                        return offload.FreshMacClaim(1)

                def release(command: list[str]) -> offload.CommandOutput:
                    self.assertEqual(command[command.index("--claim-id") + 1], identity.claim_id)
                    self.assertEqual(mac_test.command_release(waiting, self.paths), 0)
                    return offload.CommandOutput(0, "", "")

                with mock.patch.object(copies, "remote_registry", return_value=self.root / "remote-registry"), \
                        mock.patch.object(offload, "claim_for_run", side_effect=claim) as take, \
                        mock.patch.object(offload, "check_current_leftovers", return_value=offload.LeftoversEnded()) as check, \
                        mock.patch.object(controller, "capture", side_effect=release) as release_child, \
                        mock.patch.object(offload, "claimed_run", return_value=offload.PassedOnMac(1)) as run:
                    with copies.remote_host_lease("old-mac", exclusive=True), copies.remote_lease(live_mirror):
                        self.assertIsInstance(offload.run_offload(controller, config, request, time.monotonic()),
                                              offload.BusyMacDecline)
                    take.assert_not_called()
                    check.assert_not_called()
                    release_child.assert_not_called()
                    self.assertEqual(claim_state.read_run(held.path), record)
                    self.assertIsInstance(offload.run_offload(controller, config, request, time.monotonic()),
                                          offload.PassedOnMac)
                    self.assertEqual(take.call_count, 2)
                    check.assert_called_once_with(config, "old-mac", offload.ALL_MIRRORS if legacy else repo, "mac", 1)
                    release_child.assert_called_once()
                    run.assert_called_once()

    def test_claim_repository_distinguishes_the_three_history_key_cases(self) -> None:
        mirror = claim_state.mac_repository_identity(str(self.checkout))
        assert isinstance(mirror, claim_state.ResolvedMacMirror)
        history_key = "https://github.com/Team/Repository"
        supplied_key = claim_state.KnownMacHistoryKey(history_key)
        with mock.patch.object(repository, "repository_key", return_value=history_key) as read:
            supplied = mirror.claim_repository(history_key=supplied_key)
            absent = mirror.claim_repository(history_key=claim_state.UnavailableMacHistoryKey())
            read.assert_not_called()
            self.assertEqual(mirror.claim_repository(), supplied)
            read.assert_called_once_with(str(self.checkout))
            for unavailable_key in ("", store.UNKNOWN_IDENTITY):
                read.return_value = unavailable_key
                self.assertEqual(mirror.claim_repository(), absent)
        assert isinstance(supplied, claim_state.MacRepositoryIdentity)
        self.assertIs(supplied.history_key, supplied_key)
        self.assertEqual(supplied.record(), {**mirror.record(), "history_key": history_key})
        self.assertEqual(claim_state.mac_repository_record(supplied.record()), supplied)
        self.assertIsInstance(absent, claim_state.CleanupOnlyMacRepository)
        self.assertEqual(absent.record(), mirror.record())
        self.assertEqual(claim_state.mac_repository_record(absent.record()), absent)
        self.assertEqual(claim_state.mac_repository_record({**mirror.record(), "history_key": store.UNKNOWN_IDENTITY}), absent)
        for invalid_key in ("", 1):
            with self.subTest(invalid_key=invalid_key), self.assertRaisesRegex(claim_state.StateReadError, "invalid Mac history key"):
                _ = claim_state.mac_repository_record({**mirror.record(), "history_key": invalid_key})

    def test_new_remote_claim_refuses_an_identity_without_history(self) -> None:
        args = self.native_claim(self.checkout)
        args.remote = "fixture-mac"
        mirror = claim_state.mac_repository_identity(str(self.checkout))
        assert isinstance(mirror, claim_state.MacMirrorIdentity)
        identity = claim_state.mac_repository_record(mirror.record())
        self.assertIsInstance(identity, claim_state.CleanupOnlyMacRepository)
        self.assertEqual(mac_test.command_claim(
            args, self.paths, watch_claim=False, mac_repository=identity), 12)
        self.assertEqual(claim_state.read_claims(self.paths), ())

    def test_runner_keeps_the_claim_scope_after_the_checkout_origin_changes(self) -> None:
        with (self.root / "mac_test.conf").open("a") as settings:
            _ = settings.write("mirror_root=~/.local/state/mac-test/mirror\n")
        other = self.repository("source-b")
        set_origin = ["git", "-C", str(self.checkout), "remote", "set-url", "origin"]
        _ = subprocess.run([*set_origin, str(self.repo)], check=True)
        config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("fixture-mac", 1, 1),
                                           1, 1, 1, "1", {"linux_only..claude": ""})
        request = offload.OffloadRequest(self.checkout, "hana", "test", offload.NO_RESULT_FILE, (),
                                         "verify", "", target="mac")
        run_git = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)
        launch = cast(Callable[..., object], subprocess.Popen)
        origin_reads: list[str] = []

        def change_origin(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            result = run_git(command, **options)
            if command[:3] == ["git", "--git-dir", str(self.checkout / ".git")] and command[-2] == "--get-regexp":
                origin_reads.append(result.stdout.removesuffix("\0").split("\n", 1)[1])
                if len(origin_reads) == 1:
                    _ = run_git([*set_origin, str(other)], check=True)
            return result

        def claim_child(command: list[str], **options: object) -> object:
            if command[0] == "git":
                return launch(command, **options)
            self.assertEqual(command[2], "claim")
            args = mac_test.parser().parse_args(command[2:], namespace=mac_test.CliArguments())
            output = StringIO()
            with redirect_stdout(output):
                status = mac_test.command_claim(args, self.paths, watch_claim=False)
            return mock.Mock(returncode=status, communicate=mock.Mock(return_value=(output.getvalue(), "")),
                             poll=mock.Mock(return_value=status))

        controller = offload.ProcessController()
        with mock.patch.object(subprocess, "run", side_effect=change_origin), \
             mock.patch.object(subprocess, "Popen", side_effect=claim_child), \
             mock.patch.object(offload, "claimed_run", return_value=offload.PassedOnMac(1)):
            self.assertIsInstance(offload.run_offload(controller, config, request, time.monotonic()), offload.PassedOnMac)
        held, = claim_state.read_claims(self.paths)
        saved = claim_state.saved_mac_repository(held.record)
        assert isinstance(saved, claim_state.MacRepositoryIdentity)
        stem = claim_state.mac_claim_stem(REPOSITORY_KEY, 0)
        self.assertEqual((saved.mirror_name, saved.key, held.path.name),
                         (self.repo.name, REPOSITORY_KEY, f"{stem}.json"))
        self.assertEqual(saved.fence_directory, stem)
        self.assertTrue(saved.fence_generation)
        self.assertEqual(saved.history_key.key, str(self.repo))
        self.assertEqual(controller.attempt_evidence.repository_key, saved.history_key.key)
        self.assertEqual(origin_reads, [str(self.repo)])

    def test_fresh_takeover_waits_for_the_previous_hosts_cleanup_lease(self) -> None:
        with (self.root / "mac_test.conf").open("a") as settings:
            _ = settings.write("mirror_root=~/.local/state/mac-test/mirror\n")
        config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("fixture-mac", 1, 1),
                                           1, 1, 1, "1", {"linux_only.checkout": ""})
        request = offload.OffloadRequest(self.checkout, "hana", "test", offload.NO_RESULT_FILE, (),
                                         "direct", "", target="mac")
        old = offload.TakenOverMacClaim("old-mac", 1)
        with mock.patch.object(offload, "claim_for_run", return_value=old) as claim, \
                mock.patch.object(offload, "check_current_leftovers", return_value=offload.LeftoversEnded()) as check, \
                mock.patch.object(offload, "release_claim", return_value=offload.ClaimReleaseConfirmed()) as release, \
                mock.patch.object(offload, "claimed_run") as run:
            with copies.remote_host_lease(old.host, exclusive=True):
                result = offload.run_offload(offload.ProcessController(), config, request, time.monotonic())
            self.assertIsInstance(result, offload.BusyMacDecline)
            claim.assert_called_once()
            check.assert_not_called()
            release.assert_not_called()
            run.assert_not_called()

            def take_over_then_claim(current: offload.ProcessController, _config: eligibility.OffloadConfig,
                                     _request: offload.OffloadRequest, _repo: eligibility.RepositoryIdentity,
                                     _slots: tuple[int, ...], *, retire_wait: bool,
                                     mac_repository: claim_state.MacRepositoryIdentity | claim_state.NoSavedMacRepository,
                                     ) -> offload.TakenOverMacClaim | offload.FreshMacClaim:
                self.assertFalse(retire_wait)
                if claim.call_count == 2:
                    return offload.TakenOverMacClaim(config.host, 1)
                current.mac_repository = mac_repository
                return offload.FreshMacClaim(1)

            claim.side_effect = take_over_then_claim
            run.return_value = offload.PassedOnMac(1)
            result = offload.run_offload(offload.ProcessController(), config, request, time.monotonic())
            self.assertIsInstance(result, offload.PassedOnMac)
            self.assertEqual(claim.call_count, 3)
            check.assert_called_once_with(config, config.host, offload.ALL_MIRRORS, "mac", 1)
            release.assert_called_once()
            run.assert_called_once()

    def test_host_lease_contention_leaves_the_next_build_eligible(self) -> None:
        with (self.root / "mac_test.conf").open("a") as settings:
            _ = settings.write("mirror_root=~/.local/state/mac-test/mirror\n")
        config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("fixture-mac", 1, 1),
                                           1, 1, 1, "1", {"linux_only.checkout": "", "mirror_busy_backoff_s": "120"})
        request = offload.OffloadRequest(self.checkout, "hana", "test", offload.NO_RESULT_FILE, (),
                                         "direct", "", target="mac")
        controller = offload.ProcessController()

        def claim(current: offload.ProcessController, _config: eligibility.OffloadConfig,
                  _request: offload.OffloadRequest, _repo: eligibility.RepositoryIdentity, _slots: tuple[int, ...], *,
                  retire_wait: bool,
                  mac_repository: claim_state.MacRepositoryIdentity | claim_state.NoSavedMacRepository,
                  ) -> offload.FreshMacClaim:
            self.assertFalse(retire_wait)
            current.mac_repository = mac_repository
            return offload.FreshMacClaim(1)

        with mock.patch.object(offload, "claim_for_run", side_effect=claim) as take, \
                mock.patch.object(offload, "claimed_run", return_value=offload.PassedOnMac(1)) as run:
            with copies.remote_host_lease(config.host, exclusive=True):
                result = offload.run_offload(controller, config, request, time.monotonic())
            self.assertIsInstance(result, offload.BusyMacDecline)
            take.assert_not_called()
            self.assertFalse(eligibility.mirror_busy_path("checkout", "mac").exists())
            result = offload.run_offload(controller, config, request, time.monotonic())
            self.assertIsInstance(result, offload.PassedOnMac)
            take.assert_called_once()
            run.assert_called_once()

    def test_mac_history_matches_the_queue_after_a_reservation_handoff(self) -> None:
        with (self.root / "mac_test.conf").open("a") as settings:
            _ = settings.write("mirror_root=~/.local/state/mac-test/mirror\n")
        config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("fixture-mac", 1, 1),
            1, 1, 1, "1", {"abandoned_session_minutes": "1", "linux_only..claude": "", "linux_only.metadata": ""})
        separate = self.root / "separate"
        metadata = self.root / "metadata" / "repo.git"
        metadata.parent.mkdir()
        _ = subprocess.run(["git", "clone", "-q", "--separate-git-dir", str(metadata),
                            str(self.repo), str(separate)], check=True)
        with mock.patch.object(eligibility, "read_config", return_value=config):
            for checkout in (self.checkout, self.repo, separate):
                with self.subTest(checkout=checkout.name):
                    local_origin = checkout == self.checkout
                    git = ["git", "-C", str(checkout), "remote"]
                    _ = subprocess.run([*git, "set-url", "origin", str(self.repo)] if local_origin
                                       else [*git, "remove", "origin"], check=True)
                    queue_repository, _ = step_estimator.git_identity(["cargo", "check"], str(checkout))
                    assert isinstance(queue_repository, RepositoryIdentity)
                    if local_origin:
                        self.assertEqual(queue_repository.key, str(self.repo))
                    else:
                        self.assertTrue(queue_repository.key.startswith("git-root:"))
                    waiting = self.native_claim(checkout)
                    waiting.repo_root, waiting.verb, waiting.target = str(checkout), "check", "mac"
                    self.assertEqual(mac_test.command_claim_wait(waiting, self.paths), 0)
                    held, = claim_state.read_claims(self.paths)
                    saved = claim_state.saved_mac_repository(held.record)
                    assert isinstance(saved, claim_state.MacRepositoryIdentity)
                    self.assertEqual(saved.key, REPOSITORY_KEY if local_origin
                                     else "git-dir:" + str(metadata if checkout == separate else checkout / ".git"))
                    self.assertEqual(saved.history_key.key, queue_repository.key)
                    admission = offload.MacClaimFromWait(waiting.pid, claim_state.claim_identity(held.record), NoOwningSession())
                    request = offload.OffloadRequest(checkout, "hana", "test", offload.NO_RESULT_FILE, (),
                        "verify", "", target="mac", verb="check", admission=admission)
                    controller = offload.ProcessController()
                    with mock.patch.object(repository, "mac_source_repository", side_effect=AssertionError("Use saved scope")), \
                            mock.patch.object(copies, "remote_lease"), \
                            mock.patch.object(offload, "leased_offload", return_value=offload.PassedOnMac(1)):
                        self.assertIsInstance(offload.run_offload(controller, config, request, time.monotonic()),
                                              offload.PassedOnMac)
                        key = controller.attempt_evidence.repository_key
                        assert isinstance(key, str)
                        command = offload.remote_test_command(config, request,
                            eligibility.KnownRepository(saved.mirror_name, saved.key), history_key=key)
                    remote_key, = (word.removeprefix("BUILDLOG_REPO_IDENTITY=") for word in shlex.split(command)
                                   if word.startswith("BUILDLOG_REPO_IDENTITY="))
                    self.assertTrue(step_estimator.history_matches_repository({"repo_identity": remote_key}, queue_repository))
                    self.assertEqual(mac_test.command_release(waiting, self.paths), 0)

    def test_mac_worktree_rewrite_changes_history_and_preserves_the_claim_key(self) -> None:
        with (self.root / "mac_test.conf").open("a") as settings:
            _ = settings.write("mirror_root=~/.local/state/mac-test/mirror\n")
        linked = self.root / "linked"
        git = ["git", "-C", str(self.checkout)]
        _ = subprocess.run([*git, "config", "--local", "extensions.worktreeConfig", "true"], check=True)
        _ = subprocess.run([*git, "worktree", "add", "-q", "--detach", str(linked)], check=True)
        waiter = self.native_claim(linked)
        self.assertEqual(mac_test.command_claim(waiter, self.paths, watch_claim=False), 0)
        self.addCleanup(mac_test.command_release, waiter, self.paths)
        held, = claim_state.read_claims(self.paths)
        saved = claim_state.saved_mac_repository(held.record)
        assert isinstance(saved, claim_state.MacRepositoryIdentity)
        self.assertEqual(saved.history_key.key, REPOSITORY_KEY)
        rewrite = "https://github.com/Team/History"
        _ = subprocess.run(["git", "-C", str(linked), "config", "--worktree",
                           f"url.{rewrite}.git.insteadOf", ORIGIN], check=True)
        self.addCleanup(step_estimator.clear_git_identity_cache)
        queue_repository, _ = step_estimator.git_identity(["cargo", "check"], str(linked))
        assert isinstance(queue_repository, RepositoryIdentity)
        self.assertEqual(queue_repository.key, rewrite)
        scope = claim_state.RepositoryScope("checkout", REPOSITORY_KEY)
        self.assertEqual(held.scope, scope)
        self.assertEqual(claim_state.target_claim_scope(self.paths, str(linked)), scope)
        admission = offload.MacClaimFromWait(waiter.pid, claim_state.claim_identity(held.record), NoOwningSession())
        request = offload.OffloadRequest(linked, "", "check", offload.NO_RESULT_FILE, (), "verify", "",
                                         target="mac", verb="check", admission=admission)
        controller = offload.ProcessController()
        controller.mac_repository = saved
        words = self.offload_history_command(controller, request)
        key, = (word.removeprefix("BUILDLOG_REPO_IDENTITY=") for word in words
                if word.startswith("BUILDLOG_REPO_IDENTITY="))
        self.assertEqual(key, queue_repository.key)
        self.assertEqual(controller.attempt_evidence.repository_key, key)
        self.assertEqual(claim_state.read_run(held.path), held.record)

    def test_plain_ssh_linux_command_sends_the_source_repository_key(self) -> None:
        self.add_linux_target()
        origin = "https://github.com/Team/Hana"
        source = self.repository("hana", origin=origin + ".git")
        main_identity = eligibility.repository_identity(source)
        assert isinstance(main_identity, eligibility.KnownRepository)
        self.assertEqual(main_identity.key, origin)
        request = offload.OffloadRequest(source, "", "check", offload.NO_RESULT_FILE, (), "verify", "",
                                         target="linux", verb="check")
        controller = offload.ProcessController()
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(self.root / "target-state")}):
            words = self.offload_history_command(controller, request)
        self.assertEqual(words.count("--repo-identity"), 1)
        self.assertEqual(words[words.index("--repo-identity") + 1], main_identity.key)
        self.assertEqual(controller.attempt_evidence.repository_key, main_identity.key)

    def offload_history_command(self, controller: offload.ProcessController,
                                request: offload.OffloadRequest) -> list[str]:
        config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("fixture", 1, 1),
            1, 1, 1, "1", {"abandoned_session_minutes": "1", "kinds": "check", "linux_only.checkout": ""})

        def remote_command(_: copies.RemoteMirror, command: str) -> str:
            return command

        with (
            mock.patch.object(offload, "claim_for_run", return_value=offload.FreshMacClaim()),
            mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(100, "fixture")),
            mock.patch.object(offload, "copy_tree_with_key", return_value=offload.CopiedTree(offload.KnownTreeKey("tree"))),
            mock.patch.object(offload, "trusted_linux_source", return_value=True),
            mock.patch.object(offload, "expected_linux_parity", return_value={}),
            mock.patch.object(offload, "copy_tracked_scripts", return_value=True),
            mock.patch.object(copies, "remote_lease"),
            mock.patch.object(copies, "register_remote"),
            mock.patch.object(copies, "remote_use_command", side_effect=remote_command),
            mock.patch.object(offload, "release_claim", return_value=offload.ClaimReleaseConfirmed()),
            mock.patch.object(buildlog_sync, "sync_target"),
            mock.patch.object(controller, "stream_mac_run",
                return_value=offload.ReportedMacStatus(0, offload.CleanupKept())) as stream,
        ):
            result = offload.run_offload(controller, config, request, time.monotonic())
        self.assertIsInstance(result, offload.PassedOnMac if request.target == "mac" else offload.PassedOnTarget)
        stream.assert_called_once()
        arguments = cast(list[str], stream.call_args.args[0])
        return shlex.split(arguments[-1])

    def test_wait_reservation_retry_keeps_the_exact_claim_after_an_origin_change(self) -> None:
        config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("fixture-mac", 1, 1),
                                           1, 1, 1, "1", {"linux_only.checkout": ""})
        for assigned in (True, False):
            with self.subTest(assigned=assigned):
                git = ["git", "-C", str(self.checkout), "remote", "set-url", "origin"]
                _ = subprocess.run([*git, ORIGIN], check=True)
                args = build_targets.CliArguments()
                args.pid, args.repo_root = os.getpid(), str(self.checkout)
                args.target, args.kind = "mac", "check"
                args.reservation_file = str(self.root / "reservation")
                identity_file = self.root / "reservation.identity"
                args.claim_identity_file = str(identity_file)
                if assigned:
                    start = process_identity.process_start(args.pid)
                    assert isinstance(start, process_identity.Started)
                    assignment = placement.PlacementAssignment("saved-episode", "saved-attempt",
                        placement.PlacementDestination("mac", "mac", "fixture-mac",
                            claim_state.RepositoryScope("checkout", REPOSITORY_KEY)),
                        execution_identity.SourceKeyUnavailable("The fixture has no source key"),
                        placement.SubmittingOwner("fixture-host", args.pid, start.start), NoOwningSession(), 0)
                    assignment_file = self.root / "assignment.json"
                    _ = assignment_file.write_text(wire.encode_reply(wire.Assignment(assignment)))
                    args.assignment_file = str(assignment_file)
                with mock.patch.object(eligibility, "read_config", return_value=config), \
                        mock.patch.object(offload, "probe_reachability", return_value=offload.ReachableHost()):
                    self.assertEqual(build_targets.command_reserve(args), 0)
                    held, = claim_state.read_claims(self.paths)
                    claim_bytes = held.path.read_bytes()
                    identity_bytes = identity_file.read_bytes()
                    other = self.repository(f"other-{assigned}")
                    _ = subprocess.run(["git", "-C", str(other), "remote", "add", "origin",
                                        "https://example.invalid/team/other.git"], check=True)
                    _ = subprocess.run([*git, str(other)], check=True)
                    with mock.patch.object(repository, "mac_source_repository", side_effect=AssertionError("Use saved scope")), \
                            mock.patch.object(mac_test, "command_claim_wait") as claim_wait:
                        self.assertEqual(build_targets.command_reserve(args), 0)
                    claim_wait.assert_not_called()
                    self.assertEqual(claim_state.read_claims(self.paths), (held,))
                    self.assertEqual(held.path.read_bytes(), claim_bytes)
                    self.assertEqual(identity_file.read_bytes(), identity_bytes)
                    released = mac_test.release_exact_claims(self.paths, args.pid, claim_state.claim_identity(held.record), "")
                    self.assertEqual(released, mac_test.ClaimsReleased(1, ()))
                    Path(args.reservation_file).unlink()
                    identity_file.unlink()

    def test_mac_capacity_uses_the_assigned_scope_after_an_origin_change(self) -> None:
        assigned_scope = claim_state.target_claim_scope(self.paths, str(self.checkout))
        other = self.repository("other")
        _ = subprocess.run(["git", "-C", str(other), "remote", "add", "origin",
                            "https://example.invalid/team/other.git"], check=True)
        _ = subprocess.run(["git", "-C", str(self.checkout), "remote", "set-url", "origin", str(other)], check=True)
        busy = self.native_claim(self.checkout)
        self.assertEqual(mac_test.command_claim(busy, self.paths, watch_claim=False), 0)
        self.addCleanup(mac_test.command_release, busy, self.paths)
        settings = self.root / "mac_test.conf"
        self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout), "busy")
        with mock.patch.object(repository, "mac_source_repository", side_effect=AssertionError("Use the assigned scope")):
            self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout, mac_scope=assigned_scope), "free")

    def test_assigned_mac_policy_uses_the_current_checkout(self) -> None:
        assigned = claim_state.RepositoryScope("retired-mirror", REPOSITORY_KEY)
        settings = self.root / "mac_test.conf"
        with settings.open("a") as output:
            _ = output.write("linux_only.checkout=local-tests\n")
        target = target_config.SshTarget("mac", "macos", settings)
        configuration = conf.read(settings)
        self.assertEqual(build_targets.mac_target_eligibility(
            target, self.checkout, "hana", "test", target_config.ANY_ELIGIBLE_TARGET,
            answer="bad", mac_scope=assigned, configuration=configuration), target_config.EligibleTarget(target))
        self.assertEqual(build_targets.mac_target_eligibility(
            target, self.checkout, "local-tests", "test", target_config.ANY_ELIGIBLE_TARGET,
            answer="bad", mac_scope=assigned, configuration=configuration), target_config.ExcludedTarget(target, "linux_only"))

    def test_shell_mirror_refuses_to_run_or_clean_up_during_host_lease(self) -> None:
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                self.shell_mirror_contention(interrupted=interrupted)

    def shell_mirror_contention(self, *, interrupted: bool) -> None:
        mirror = copies.RemoteMirror("fixture", copies.SelectedRepository("checkout"),
                                     "fixture-mac", "/fixture/checkout", "fixture-session")
        copies.register_remote(mirror)
        args = mac_queue_run.Arguments()
        args.worktree, args.lifecycle_script, args.argv = self.checkout, "fixture-callback", ["true"]
        args.no_wait = True

        def callback(_script: str, phase: str, argv_file: Path, _argv: list[str],
                     _environment: dict[str, str], **_options: object) -> int:
            if phase == "prepare":
                _ = argv_file.write_text('["true"]')
                _ = argv_file.with_suffix(".json.lease").write_text(str(copies.remote_lease_path(mirror)))
                if interrupted:
                    raise KeyboardInterrupt
            else:
                self.assertEqual(phase, "cleanup")
                with self.assertRaises(copies.BusyRemoteHost), copies.remote_host_lease(mirror.host, exclusive=True):
                    self.fail("Shell cleanup did not hold the host lease")
                with self.assertRaises(copies.BusyRemoteMirror), copies.remote_lease(mirror):
                    self.fail("Shell cleanup did not hold the mirror lease")
            return 0

        run_process = subprocess.CompletedProcess(["fixture-admission"], 0, stdout="")
        with mock.patch.object(Path, "home", return_value=self.root), \
                mock.patch.object(mac_queue_run, "owning_session", return_value=NoOwningSession()), \
                mock.patch.object(mac_queue_run, "admission_command", return_value=["fixture-admission"]), \
                mock.patch.object(mac_queue_run, "capture_command",
                                  return_value=(0, "a\tb\tfixture-mac\td\tepisode\tattempt", "")), \
                mock.patch.object(mac_queue_run, "run_callback", side_effect=callback) as lifecycle, \
                mock.patch.object(mac_queue_run, "stream_remote") as run, \
                mock.patch.object(subprocess, "run", return_value=run_process):
            with copies.remote_host_lease(mirror.host, exclusive=True):
                if interrupted:
                    with self.assertRaises(KeyboardInterrupt):
                        _ = mac_queue_run.main(["--what", "fixture", "--kind", "run", "--package", "hana",
                                                "--worktree", str(args.worktree), "--no-wait",
                                                "--lifecycle-script", args.lifecycle_script, "--", "true"])
                else:
                    self.assertEqual(mac_queue_run.run(args), 2)
                mac_queue_run.clean_ended_runs({})
            run.assert_not_called()
            self.assertEqual([call.args[1] for call in lifecycle.call_args_list], ["prepare"])
            mac_queue_run.clean_ended_runs({})
            self.assertEqual([call.args[1] for call in lifecycle.call_args_list], ["prepare", "cleanup"])
            self.assertEqual(list((self.root / mac_queue_run.RUNS).iterdir()), [])

    def test_guest_refuses_a_mirror_name_with_a_tab_before_writing_state(self) -> None:
        checkout = self.repository("with\ta-tab")
        with self.assertRaises(mac_run.MacRunRefused):
            with mac_run.held(checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                self.fail("The guest used an invalid mirror name")
        self.assertFalse(self.paths.directory.exists())

    def test_guest_probe_and_claim_share_the_scope_without_an_origin(self) -> None:
        _ = subprocess.run(["git", "-C", str(self.checkout), "remote", "remove", "origin"], check=True)
        settings = self.root / "mac_test.conf"
        native = self.native_claim(self.repo)
        self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
        self.addCleanup(mac_test.command_release, native, self.paths)

        self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout, mac_kind=mac_test.MacClaimKind.GUEST),
                         "free")
        with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
            self.assertEqual({claim.scope for claim in claim_state.read_claims(self.paths)},
                             {claim_state.RepositoryScope(".claude", REPOSITORY_KEY),
                              claim_state.RepositoryScope("checkout", "git-dir:" + str(self.checkout / ".git"), 1)})
            self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout, mac_kind=mac_test.MacClaimKind.GUEST),
                             "busy")
            with self.assertRaises(mac_run.MacRunRefused):
                with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                    self.fail("The guest exceeded the checkout's limit")

    def test_guest_probe_checks_failed_mac_probe(self) -> None:
        settings = self.root / "mac_test.conf"
        self.paths.directory.mkdir()
        self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout, mac_kind=mac_test.MacClaimKind.GUEST),
                         "free")
        (self.paths.directory / "failed_probe").touch()
        self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout, mac_kind=mac_test.MacClaimKind.GUEST),
                         "bad")

    def test_claim_count_uses_the_repository_limit_and_host_default(self) -> None:
        self.configure_limit(2)
        scope = claim_state.RepositoryScope(".claude", REPOSITORY_KEY)
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
                _ = claims.enter_context(mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"))
                if first == "guest":
                    guest, = claim_state.read_claims(self.paths)
                    self.assertEqual(guest.scope, claim_state.RepositoryScope("checkout", REPOSITORY_KEY, 2))
                    start_native()
                self.assertEqual({claim.scope for claim in claim_state.read_claims(self.paths)},
                                 {scope, claim_state.RepositoryScope("checkout", REPOSITORY_KEY, 2)})
                with self.assertRaises(mac_run.MacRunRefused):
                    with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                        self.fail("A third run exceeded the configured limit")
            self.assertEqual(claim_state.read_claims(self.paths), ())

    def test_native_starts_when_either_guest_releases_a_slot(self) -> None:
        self.configure_limit(2)
        settings = self.root / "mac_test.conf"
        for ends_first in (0, 1):
            with self.subTest(ends_first=ends_first), ExitStack() as claims:
                guests = [claims.enter_context(ExitStack()) for _ in range(2)]
                for guest in guests:
                    _ = guest.enter_context(mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"))
                self.assertEqual({claim.scope for claim in claim_state.read_claims(self.paths)},
                                 {claim_state.RepositoryScope("checkout", REPOSITORY_KEY, slot) for slot in (1, 2)})
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
                with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
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

        with mac_run.held(self.repository("hana", origin=HANA_ORIGIN), "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
            claims = {claim.scope: claim for claim in claim_state.read_claims(self.paths)}
            numbered, = [claim for scope, claim in claims.items()
                         if isinstance(scope, claim_state.RepositoryScope) and scope.name == "hana" and scope.slot == 2]
            self.assertEqual(claims[original.scope], original)
            self.assertNotEqual(numbered.path, original.path)
            self.assertEqual(original.path.read_bytes(), original_data)
        self.assertEqual(claim_state.read_claims(self.paths), (original,))

    def test_legacy_numbered_claim_keeps_its_file_until_release(self) -> None:
        with (self.root / "build_folders.conf").open("a") as settings:
            _ = settings.write("# whole: 1..\nslots.fixture-mac.hana.max=2\n")
        other = self.native_claim(self.repository("hana.slot-2"))
        with mac_run.held(self.repository("hana", origin=HANA_ORIGIN), "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
            claim, = claim_state.read_claims(self.paths)
            legacy = claim.path.with_name("hana.slot-2.json")
            record = claim.record.copy()
            _ = record.pop("mac_repository")
            _ = record.pop("repo_key")
            mac_test.write_json(legacy, record)
            claim.path.unlink()
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
            with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                self.fail("A guest started with unreadable state")
        self.assertEqual(output.getvalue(), "")

    def test_mac_activity_watcher_leaves_guest_claim_for_its_holder(self) -> None:
        with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
            claim, = claim_state.read_claims(self.paths)
            config = mac_test.MacTestConfig("", "", "", "", 1, 1, 1, 1, 1)
            with mock.patch.object(mac_test, "check_mac_activity", return_value=mac_test.MacIdle()) as probe:
                self.assertFalse(mac_test.claim_watch_pass(self.paths, config))
            probe.assert_not_called()
            self.assertEqual(claim_state.read_claims(self.paths), (claim,))
        self.assertEqual(claim_state.read_claims(self.paths), ())

    def keep_guest_claim(self) -> claim_state.HeldClaim:
        armed = mac_linux.ArmedGuestAttempt.from_record(ARMED_GUEST_RECORD)
        failure = mac_linux.GuestEndUnproven("The guest end is unproven", armed)
        with self.assertRaises(mac_linux.GuestEndUnproven) as raised:
            with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED,
                              attempt=ARMED_GUEST_RECORD["attempt"]):
                raise failure
        self.assertIs(raised.exception, failure)
        claim, = claim_state.read_claims(self.paths)
        self.assertEqual(claim.record.get("guest_armed"), ARMED_GUEST_RECORD)
        return claim

    def test_guest_claim_records_the_attempt_and_releases_on_other_exits(self) -> None:
        def run() -> None:
            with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="given-attempt"):
                claim, = claim_state.read_claims(self.paths)
                identity = claim_state.claim_identity(claim.record)
                self.assertTrue(identity.claim_id.startswith(mac_test.MAC_LINUX_CLAIM_ID_PREFIX))
                self.assertEqual(identity.attempt, "given-attempt")
                if failure:
                    raise RuntimeError("fixture run failed")

        for failure in (False, True):
            with self.subTest(failure=failure):
                if failure:
                    with self.assertRaisesRegex(RuntimeError, "fixture run failed"):
                        run()
                else:
                    run()
                self.assertEqual(claim_state.read_claims(self.paths), ())

    def test_unproven_guest_claim_blocks_the_next_guest_attempt(self) -> None:
        claim = self.keep_guest_claim()
        with mock.patch.object(mac_linux, "prove_kept_guest_end", create=True,
                               return_value=mac_linux.GuestEndPending("guest offline")) as prove:
            with self.assertRaisesRegex(mac_run.MacRunRefused, "needs cleanup.*guest offline"):
                with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="next-guest"):
                    self.fail("The next guest started without proof")
        prove.assert_called_once_with(mac_linux.ArmedGuestAttempt.from_record(ARMED_GUEST_RECORD))
        self.assertEqual(claim_state.read_claims(self.paths), (claim,))

    def test_generic_cleanup_and_takeover_keep_guest_claim_after_owner_ends(self) -> None:
        claim = self.keep_guest_claim()
        with mock.patch.object(process_identity, "process_start", return_value=process_identity.Started("new-start")):
            with claim_state.locked(self.paths.lock):
                self.assertEqual(mac_test.live_claims(self.paths), (claim,))
            identity = claim_state.claim_identity(claim.record)
            self.assertEqual(tuple(claim_state.read_wait_reservations(self.paths, identity, claim.path)), ())
            self.assertIsInstance(mac_test.release_exact_claims(self.paths, claim.record["pid"], identity, ""),
                                  mac_test.ClaimReleaseRefused)
            waiting = self.native_claim(self.checkout)
            waiting.remote = "fixture-mac"
            self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False), 11)
        self.assertEqual(claim_state.read_claims(self.paths), (claim,))

    def test_proven_guest_end_releases_the_exact_claim_once(self) -> None:
        _ = self.keep_guest_claim()

        def prove(armed: mac_linux.ArmedGuestAttempt) -> mac_linux.GuestEndProven:
            self.assertEqual(armed.to_record(), ARMED_GUEST_RECORD)
            self.assertEqual(claim_state.coordination_depth.get(), 0)
            return mac_linux.GuestEndProven()

        with mock.patch.object(mac_linux, "prove_kept_guest_end", side_effect=prove, create=True) as checked:
            with self.assertRaisesRegex(mac_run.MacRunRefused, "needs cleanup"):
                with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="refused"):
                    self.fail("Cleanup granted permission for the refused attempt")
            self.assertEqual(claim_state.read_claims(self.paths), ())
            with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="after-proof"):
                self.assertEqual(len(claim_state.read_claims(self.paths)), 1)
        checked.assert_called_once_with(mac_linux.ArmedGuestAttempt.from_record(ARMED_GUEST_RECORD))

    def test_unreadable_guest_proof_keeps_the_claim(self) -> None:
        claim = self.keep_guest_claim()
        with mock.patch.object(mac_linux, "prove_kept_guest_end", create=True,
                               side_effect=OSError("cannot read proof")) as prove:
            with self.assertRaisesRegex(mac_run.MacRunRefused, "needs cleanup.*cannot read proof"):
                with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="next-guest"):
                    self.fail("The next guest started without a readable proof")
        prove.assert_called_once_with(mac_linux.ArmedGuestAttempt.from_record(ARMED_GUEST_RECORD))
        self.assertEqual(claim_state.read_claims(self.paths), (claim,))

    def test_unreadable_armed_guest_record_keeps_the_claim(self) -> None:
        claim = self.keep_guest_claim()
        damaged = {**claim.record, "guest_armed": {"attempt": ARMED_GUEST_RECORD["attempt"]}}
        mac_test.write_json(claim.path, damaged)
        with mock.patch.object(mac_linux, "prove_kept_guest_end", create=True) as prove:
            with self.assertRaisesRegex(mac_run.MacRunRefused, "needs cleanup"):
                with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="next-guest"):
                    self.fail("The guest started with an unreadable armed record")
        prove.assert_not_called()
        saved, = claim_state.read_claims(self.paths)
        self.assertEqual(saved.record, damaged)

    def test_guest_proof_does_not_release_a_reused_slot(self) -> None:
        claim = self.keep_guest_claim()
        replacement = {**claim.record, "claim_id": "replacement", "attempt": "new-attempt"}
        del replacement["guest_armed"]

        def prove(_armed: mac_linux.ArmedGuestAttempt) -> mac_linux.GuestEndProven:
            with claim_state.locked(self.paths.lock):
                mac_test.write_json(claim.path, replacement)
            return mac_linux.GuestEndProven()

        with mock.patch.object(mac_linux, "prove_kept_guest_end", side_effect=prove, create=True):
            with self.assertRaisesRegex(mac_run.MacRunRefused, "needs cleanup"):
                with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED, attempt="refused"):
                    self.fail("Cleanup granted permission for the refused attempt")
        saved, = claim_state.read_claims(self.paths)
        self.assertEqual(saved.record, replacement)

    def test_release_errors_reach_the_caller_after_the_guest_run(self) -> None:
        refused = mac_test.ClaimReleaseRefused("the exact claim changed during cleanup")
        with mock.patch.object(mac_test, "release_exact_claims", return_value=refused):
            with self.assertRaisesRegex(RuntimeError, refused.reason):
                with mac_run.held(self.checkout, "mac", StepJob("test-affected"), GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                    self.assertEqual(len(claim_state.read_claims(self.paths)), 1)

    def test_release_error_preserves_the_guest_error(self) -> None:
        refused = mac_test.ClaimReleaseRefused("the exact claim changed during cleanup")
        with mock.patch.object(mac_test, "release_exact_claims", return_value=refused):
            with self.assertRaisesRegex(ValueError, "guest failed") as failed:
                with mac_run.held(self.checkout, "mac", StepJob("test-affected"), GATE_JOB_NOT_RECORDED, attempt="fixture-attempt"):
                    raise ValueError("guest failed")
        self.assertEqual(failed.exception.__notes__, [f"Mac claim release failed: {refused.reason}"])

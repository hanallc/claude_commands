"""Check mac-linux claims against the native Mac claim path."""
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
from typing import cast, final, override
import unittest
from unittest import mock

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_queue import execution_identity, mac_run, placement
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, StepJob
from scripts.buildlog import repository
from scripts.build_targets import build_targets, target_config
from scripts.direct import phase_wait_holder
from scripts.lib import process_start as process_identity
from scripts.mac_test import claim_state, eligibility, mac_test, offload
from scripts.production.gate_routes import SuiteMode


ORIGIN = "https://github.com/hanallc/claude_commands.git"
REPOSITORY_KEY = ORIGIN.removesuffix(".git")


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
        _ = subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", ORIGIN], check=True)
        self.checkout = self.root / "checkout"
        _ = subprocess.run(["git", "clone", "-q", str(self.repo), str(self.checkout)], check=True)
        _ = subprocess.run(["git", "-C", str(self.checkout), "remote", "set-url", "origin",
                            ORIGIN], check=True)
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
            self.assertEqual(claim.scope, claim_state.RepositoryScope("checkout", REPOSITORY_KEY, 1))
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

            waited = mock.Mock(side_effect=release_guest)
            with mock.patch.object(mac_test, "time", mock.Mock(wraps=time, sleep=waited)):
                self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
            waited.assert_called_once()

        with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with native Mac run"):
            with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
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
            with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
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
        other = self.repository("hana")
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
                    with mac_run.held(other, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
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
        with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
            self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False, assignment=assignment), 11)

        resolve = repository.mac_source_repository

        def resolve_outside_lock(common: str) -> tuple[str, str]:
            self.assertEqual(claim_state.coordination_depth.get(), 0)
            return resolve(common)

        with mock.patch.object(repository, "mac_source_repository", side_effect=resolve_outside_lock) as lookup:
            self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False, assignment=assignment), 0)
        lookup.assert_called_once()
        self.addCleanup(mac_test.command_release, waiting, self.paths)
        reserved, = claim_state.read_claims(self.paths)
        self.assertEqual(reserved.scope, claim_state.RepositoryScope("checkout", REPOSITORY_KEY))
        self.assertTrue(reserved.matches_mac_assignment_scope(old_scope))

    def test_kept_claim_takeover_and_cleanup_use_the_saved_key(self) -> None:
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
                "replacement-mac", 1, 1)), mock.patch.object(offload, "read_leftovers", side_effect=local_ssh):
            args = build_targets.CliArguments()
            args.pid, args.repo_root = owner.pid, str(self.repo)
            build_targets.cleanup_refused_claims(target_config.SshTarget("mac", "macos", config), args,
                                               session=NoOwningSession())
        self.assertTrue(marker.exists())
        self.assertEqual(claim_state.read_claims(self.paths), ())

    def test_mac_local_origins_follow_the_upstream_repository(self) -> None:
        source = claim_state.mac_repository_identity(str(self.repo))
        assert isinstance(source, claim_state.MacRepositoryIdentity)
        self.assertEqual(source.key, REPOSITORY_KEY)
        for origin in (str(self.repo), "../.claude"):
            with self.subTest(origin=origin):
                _ = subprocess.run(["git", "-C", str(self.checkout), "remote", "set-url", "origin", origin], check=True)
                self.assertEqual(claim_state.mac_repository_identity(str(self.checkout)), source)
        native = self.native_claim(self.repo)
        self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
        self.addCleanup(mac_test.command_release, native, self.paths)
        with self.assertRaises(mac_run.MacRunRefused), mac_run.held(
                self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
            self.fail("The clone exceeded the repository limit")

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
        mac_test.write_json(path, {"pid": os.getpid(), "proc_start": "ended", "what": "old check",
            "worktree": str(self.checkout), "since": "2026-11-21T10:00:00+00:00",
            "claim_id": "old", "episode": "old-episode", "attempt": "old-attempt", "submitting_session": "",
            "remote_host": "fixture-mac", "repo_name": "checkout"})
        other = self.native_claim(self.repo)
        other.remote = "fixture-mac"
        self.assertEqual(mac_test.command_claim(other, self.paths, watch_claim=False), 11)
        self.assertEqual(phase_wait_holder.mac_owner("mac", str(self.repo)),
                         ("--holder-unit", "old check (kept for cleanup)"))
        waiting = self.native_claim(self.checkout)
        self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False), 13)
        saved, = claim_state.read_claims(self.paths)
        self.assertEqual(saved.path, path)
        self.assertEqual(saved.scope, claim_state.WHOLE_MAC)
        self.assertEqual(saved.record["proc_start"], start.start)
        self.assertEqual(saved.record["claim_id"], "old")
        runner = self.native_claim(self.checkout)
        runner.remote, runner.from_pid = "fixture-mac", waiting.pid
        runner.claim_id, runner.episode, runner.attempt = "old", "old-episode", "old-attempt"
        with mock.patch.object(repository, "mac_source_repository", side_effect=AssertionError("No Git on handoff")):
            self.assertEqual(mac_test.command_claim(runner, self.paths, watch_claim=False), 13)
        self.assertEqual(mac_test.command_release(runner, self.paths), 0)

    def test_offload_adoption_uses_the_reservation_file_for_its_fence(self) -> None:
        saved = claim_state.mac_repository_identity(str(self.checkout))
        assert isinstance(saved, claim_state.MacRepositoryIdentity)
        waiting = self.native_claim(self.checkout)
        self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False), 0)
        reserved, = claim_state.read_claims(self.paths)
        legacy = reserved.path.with_name("checkout.json")
        record = reserved.record.copy()
        _ = record.pop("mac_repository")
        mac_test.write_json(legacy, record)
        reserved.path.unlink()
        identity = claim_state.claim_identity(record)
        request = offload.OffloadRequest(self.checkout, "hana", "test", offload.NO_RESULT_FILE, (),
            "verify", "", target="mac", admission=offload.MacClaimFromWait(waiting.pid, identity, NoOwningSession()))
        config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("fixture-mac", 1, 1),
                                           1, 1, 1, "1", {})
        controller = offload.ProcessController()

        def claim_child(command: list[str], **_options: object) -> mock.Mock:
            args = mac_test.parser().parse_args(command[2:], namespace=mac_test.CliArguments())
            output = StringIO()
            with redirect_stdout(output):
                status = mac_test.command_claim(args, self.paths, watch_claim=False)
            return mock.Mock(returncode=status, communicate=mock.Mock(return_value=(output.getvalue(), "")),
                             poll=mock.Mock(return_value=status))

        self.addCleanup(mac_test.command_release, waiting, self.paths)
        with mock.patch.object(subprocess, "Popen", side_effect=claim_child), \
                mock.patch.object(repository, "mac_source_repository", side_effect=AssertionError("No second lookup")):
            answer = offload.claim_for_run(controller, config, request,
                eligibility.KnownRepository("checkout", saved.key), (1,), mac_repository=saved)
        self.assertIsInstance(answer, offload.FreshMacClaim)
        held, = claim_state.read_claims(self.paths)
        self.assertEqual(held.path, legacy)
        self.assertEqual(controller.claim_scope_key, legacy.stem)
        self.assertEqual(controller.mac_repository, claim_state.saved_mac_repository(held.record))
        self.assertEqual(offload.start_fence_relative(identity, "mac",
                         scope_key=controller.claim_scope_key).parent.name, "checkout")
        pending = offload.pending_start_path("mac", identity)
        pending.parent.mkdir()
        pending.touch()
        settled = offload.LeftoverObservation(offload.LeftoversEnded(),
            "dispatch_start_settled=" + offload.pending_start_key(identity))
        with mock.patch.object(eligibility, "read_connection_settings"), \
                mock.patch.object(offload, "read_leftovers", return_value=settled) as cleanup:
            self.assertEqual(mac_test.command_release(waiting, self.paths), 0)
        self.assertIn(offload.start_fence_command(identity, "mac", settle=True, scope_key=legacy.stem),
                      cast(str, cleanup.call_args.args[2]))
        self.assertFalse(pending.exists())

    def test_guest_probe_and_claim_share_the_scope_without_an_origin(self) -> None:
        _ = subprocess.run(["git", "-C", str(self.checkout), "remote", "remove", "origin"], check=True)
        settings = self.root / "mac_test.conf"
        native = self.native_claim(self.repo)
        self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
        self.addCleanup(mac_test.command_release, native, self.paths)

        self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout, mac_kind=mac_test.MacClaimKind.GUEST),
                         "free")
        with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
            self.assertEqual({claim.scope for claim in claim_state.read_claims(self.paths)},
                             {claim_state.RepositoryScope(".claude", REPOSITORY_KEY),
                              claim_state.RepositoryScope("checkout", "git-dir:" + str(self.checkout / ".git"), 1)})
            self.assertEqual(mac_test.free_now(self.paths, settings, self.checkout, mac_kind=mac_test.MacClaimKind.GUEST),
                             "busy")
            with self.assertRaises(mac_run.MacRunRefused):
                with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
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
                _ = claims.enter_context(mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED))
                if first == "guest":
                    guest, = claim_state.read_claims(self.paths)
                    self.assertEqual(guest.scope, claim_state.RepositoryScope("checkout", REPOSITORY_KEY, 2))
                    start_native()
                self.assertEqual({claim.scope for claim in claim_state.read_claims(self.paths)},
                                 {scope, claim_state.RepositoryScope("checkout", REPOSITORY_KEY, 2)})
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

        with mac_run.held(self.repository("hana"), "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
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
        with mac_run.held(self.repository("hana"), "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
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

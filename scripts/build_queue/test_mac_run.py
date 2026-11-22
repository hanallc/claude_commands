"""Check mac-linux claims against the native Mac claim path."""
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
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
from typing import cast, final, override
import unittest
from unittest import mock

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_queue import execution_identity, mac_run, placement, step_estimator
from scripts.build_queue.admission_records import RepositoryIdentity
from scripts.build_queue import admission_wire as wire
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, StepJob
from scripts.buildlog import repository
from scripts.build_targets import build_targets, target_config
from scripts.copies import copies
from scripts.direct import phase_wait_holder
from scripts.lib import conf, process_start as process_identity
from scripts.mac_test import claim_state, eligibility, mac_queue_run, mac_test, offload
from scripts.production.gate_routes import SuiteMode


ORIGIN = "https://github.com/hanallc/claude_commands.git"
REPOSITORY_KEY = ORIGIN.removesuffix(".git")
HANA_ORIGIN = "https://fixture.invalid/team/hana.git"


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

    def repository(self, name: str, *, origin: str = "") -> Path:
        path = self.root / name
        _ = subprocess.run(["git", "init", "-q", str(path)], check=True)
        if origin:
            _ = subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
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

        def resolve_outside_lock(worktree: str, common: str) -> repository.MacSourceRepository:
            self.assertEqual(claim_state.coordination_depth.get(), 0)
            return resolve(worktree, common)

        with mock.patch.object(repository, "mac_source_repository", side_effect=resolve_outside_lock) as lookup:
            self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False, assignment=assignment), 0)
        lookup.assert_called_once()
        self.addCleanup(mac_test.command_release, waiting, self.paths)
        reserved, = claim_state.read_claims(self.paths)
        self.assertEqual(reserved.scope, claim_state.RepositoryScope("checkout", REPOSITORY_KEY))
        self.assertTrue(reserved.matches_mac_assignment_scope(old_scope))

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
        with mac_run.held(worktrees[0], "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
            with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with mac-linux"):
                with mac_run.held(worktrees[1], "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
                    self.fail("The second guest took the shared slot")
        with mac_run.held(worktrees[1], "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
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
                self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
            self.fail("The clone exceeded the repository limit")

    def test_separate_git_directory_with_a_relative_origin_shares_capacity(self) -> None:
        separate = self.root / "separate"
        metadata = self.root / "metadata" / "git" / "repo.git"
        metadata.parent.mkdir(parents=True)
        _ = subprocess.run(["git", "clone", "-q", "--separate-git-dir", str(metadata),
                            str(self.repo), str(separate)], check=True)
        for checkout, origin in ((separate, "../.claude"), (self.checkout, "../separate")):
            _ = subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "origin", origin], check=True)

        linked = self.root / "linked" / "worktree"
        _ = subprocess.run(["git", "-C", str(separate), "config", "core.worktree", str(separate)], check=True)
        _ = subprocess.run(["git", "-C", str(separate), "worktree", "add", "-q", "--detach", str(linked)], check=True)
        native = self.native_claim(self.repo)
        self.assertEqual(mac_test.command_claim(native, self.paths, watch_claim=False), 0)
        self.addCleanup(mac_test.command_release, native, self.paths)
        for checkout in (separate, self.checkout, linked):
            with self.subTest(checkout=checkout.name):
                self.assertEqual(claim_state.target_claim_scope(self.paths, str(checkout)),
                                 claim_state.RepositoryScope(".claude", REPOSITORY_KEY))
                with self.assertRaisesRegex(mac_run.MacRunRefused, "busy with native Mac run"):
                    with mac_run.held(checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
                        self.fail("The guest took the native run's place")

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
                    saved = claim_state.saved_mac_repository(fresh.record)
                    assert isinstance(saved, claim_state.MacRepositoryIdentity)
                    self.assertEqual(saved.mirror_name, "source-b" if changed else ".claude")
                    self.assertEqual(saved.history_key, queue_repository.key)
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
                with mac_run.held(self.checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
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

    def test_interrupted_replacement_retires_the_wait_reservation(self) -> None:
        self.exercise_interrupted_replacement(("inspect", "retry", "release"))

    def test_status_keeps_an_interrupted_replacement_available_to_the_wait_owner(self) -> None:
        self.exercise_interrupted_replacement(("status-direct", "status-assigned"))

    def test_failed_retirement_keeps_the_reservation_and_allows_an_unrelated_claim(self) -> None:
        self.exercise_interrupted_replacement(("unlink-error",))

    def exercise_interrupted_replacement(self, recoveries: tuple[str, ...]) -> None:
        script = """
import os
from pathlib import Path
import sys
from unittest.mock import patch

from scripts.mac_test import mac_test

path = Path(sys.argv[1])
args = mac_test.parser().parse_args(sys.argv[3:], namespace=mac_test.CliArguments())
if sys.argv[2] == "ended-owner":
    args.pid = os.getpid()
unlink = Path.unlink

def interrupt(self, *args, **kwargs):
    if self == path:
        os._exit(71)
    return unlink(self, *args, **kwargs)

with patch.object(Path, "unlink", interrupt):
    sys.exit(mac_test.command_claim(args, mac_test.state_paths("mac"), watch_claim=False, session=args.session))
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
                if recovery == "release":
                    waiting.episode, waiting.attempt = "episode", "attempt"
                    waiting.claim_id = execution_identity.assigned_claim_id(waiting.episode, waiting.attempt)
                reservation = saved if recovery == "unlink-error" else claim_state.WHOLE_MAC
                self.assertEqual(mac_test.command_claim(waiting, self.paths, watch_claim=False,
                                                       mac_repository=reservation), 0)
                reserved, = claim_state.read_claims(self.paths)
                old_path = reserved.path
                if recovery == "release":
                    self.paths.claims.mkdir(exist_ok=True)
                    old_path = old_path.rename(self.paths.claims / "zz-legacy.json")
                elif recovery == "unlink-error":
                    old_path = old_path.rename(self.paths.claims / "legacy.json")
                    incomplete = reserved.record.copy()
                    del incomplete["mac_repository"]
                    mac_test.write_json(old_path, incomplete)
                reserved, = claim_state.read_claims(self.paths)
                self.assertEqual(reserved.path, old_path)
                identity = claim_state.claim_identity(reserved.record)
                scope = claim_state.RepositoryScope(saved.mirror_name, saved.key)
                assigned = placement.PlacementAssignment("episode", "attempt",
                    placement.PlacementDestination("mac", "mac", "fixture-mac", scope),
                    execution_identity.SourceKeyUnavailable("The fixture has no source key"),
                    placement.SubmittingOwner("fixture-host", waiting.pid, reserved.record["proc_start"]),
                    NoOwningSession(), 0)
                route = (offload.DirectOffloadRequest() if recovery == "status-direct"
                         else offload.AssignedOffloadRequest(assigned))
                request = offload.OffloadRequest(self.checkout, "hana", "test", offload.NO_RESULT_FILE, (),
                    "verify", "", target="mac", placement=route,
                    admission=offload.MacClaimFromWait(waiting.pid, identity, NoOwningSession()))
                command = offload.claim_arguments(request, config, retire_wait=True, mac_repository=saved)
                owner = "current-owner" if recovery in ("inspect", "release") else "ended-owner"
                child = subprocess.run([sys.executable, "-c", script, str(reserved.path), owner, *command[2:]],
                                       capture_output=True, text=True, timeout=5)
                self.assertEqual(child.returncode, 71, child.stdout + child.stderr)
                self.assertEqual(len(claim_state.read_claims(self.paths)), 2)
                if recovery.startswith("status-"):
                    self.check_replacement_after_status(request, config, reserved.path, script)
                    continue
                if recovery == "unlink-error":
                    self.check_unrelated_acquisition_after_failed_retirement(reserved.path)
                    continue
                expected = claim_state.ClaimIdentity(execution_identity.assigned_claim_id(assigned.episode, assigned.attempt),
                                                     assigned.episode, assigned.attempt)
                if recovery == "inspect":
                    controller = offload.ProcessController()
                    controller.placement = request.placement
                    inspection = offload.LocalClaimAttempt()
                    controller.inspect_claim(inspection, expected)
                    self.assertIsInstance(inspection.answer, offload.FreshMacClaim)
                    self.assertEqual(controller.claim_identity, expected)
                elif recovery == "retry":
                    retry = mac_test.parser().parse_args(command[2:], namespace=mac_test.CliArguments())
                    self.assertEqual(mac_test.command_claim(retry, self.paths, watch_claim=False), 13)
                release = mac_test.CliArguments()
                release.pid = os.getpid()
                release.claim_id, release.episode, release.attempt = expected.claim_id, expected.episode, expected.attempt
                if recovery == "release":
                    self.assertEqual(mac_test.command_release(release, self.paths), 0)
                    self.assertEqual(claim_state.read_claims(self.paths), ())
                    continue
                kept, = claim_state.read_claims(self.paths)
                self.assertFalse(reserved.path.exists())
                self.assertEqual(claim_state.claim_identity(kept.record), expected)
                if recovery == "retry":
                    self.assertTrue(kept.record.get("cleanup_pending"))
                    self.assertEqual(kept.record["pid"], os.getpid())
                self.assertEqual(mac_test.command_release(release, self.paths), 0)

    def check_replacement_after_status(self, request: offload.OffloadRequest, config: eligibility.OffloadConfig,
                                       old_path: Path, script: str) -> None:
        before = next(claim for claim in claim_state.read_claims(self.paths) if claim.path != old_path)
        status_config = mac_test.MacTestConfig("", "", "", "", 1, 0, 1, 1, 1)
        self.assertEqual(mac_test.command_status(self.paths, status_config), 0)
        self.assertFalse(old_path.exists())
        self.assertEqual(claim_state.read_claims(self.paths), (before,))
        controller = offload.ProcessController()
        original_arguments = offload.claim_arguments

        def private_claim(*args: object, **kwargs: object) -> list[str]:
            make_arguments = cast(Callable[..., list[str]], original_arguments)
            command = make_arguments(*args, **kwargs)
            return [sys.executable, "-c", script, "", "current-owner", *command[2:]]

        def release(command: list[str]) -> offload.CommandOutput:
            args = mac_test.parser().parse_args(command[2:], namespace=mac_test.CliArguments())
            return offload.CommandOutput(mac_test.command_release(args, self.paths), "", "")

        def run(_controller: offload.ProcessController, *_args: object) -> offload.PassedOnMac:
            check.assert_called_once()
            claim, = claim_state.read_claims(self.paths)
            self.assertFalse(claim.record.get("cleanup_pending"))
            self.assertEqual(claim.record["pid"], os.getpid())
            return offload.PassedOnMac(1)

        with mock.patch.object(offload, "claim_arguments", side_effect=private_claim), \
                mock.patch.object(controller, "capture", side_effect=release), \
                mock.patch.object(offload, "check_current_leftovers", return_value=offload.LeftoversEnded()) as check, \
                mock.patch.object(offload, "claimed_run", side_effect=run) as launched:
            result = offload.run_offload(controller, config, request, time.monotonic())
        self.assertIsInstance(result, offload.PassedOnMac)
        check.assert_called_once()
        launched.assert_called_once()
        identity = controller.claim_identity
        assert isinstance(identity, claim_state.ClaimIdentity)
        args = mac_test.CliArguments()
        args.pid = os.getpid()
        args.claim_id, args.episode, args.attempt = identity.claim_id, identity.episode, identity.attempt
        self.assertEqual(mac_test.command_release(args, self.paths), 0)

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

    def test_runner_uses_one_checkout_origin_for_the_saved_claim(self) -> None:
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
            if command[:3] == set_origin[:3] and command[-3:] == ["remote", "get-url", "origin"]:
                origin_reads.append(result.stdout.strip())
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
        self.assertEqual((saved.mirror_name, saved.key, held.path.name, saved.fence_directory),
                         (self.repo.name, REPOSITORY_KEY, f"{stem}.json", stem))
        self.assertEqual(saved.history_key, str(self.repo))
        self.assertEqual(controller.attempt_evidence.repository_key, saved.history_key)
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
                    with mock.patch.object(repository, "repository_key", side_effect=AssertionError("Use the checkout origin")):
                        self.assertEqual(mac_test.command_claim_wait(waiting, self.paths), 0)
                    held, = claim_state.read_claims(self.paths)
                    saved = claim_state.saved_mac_repository(held.record)
                    assert isinstance(saved, claim_state.MacRepositoryIdentity)
                    self.assertEqual(saved.key, REPOSITORY_KEY if local_origin
                                     else "git-dir:" + str(metadata if checkout == separate else checkout / ".git"))
                    self.assertEqual(saved.history_key, queue_repository.key)
                    admission = offload.MacClaimFromWait(waiting.pid, claim_state.claim_identity(held.record), NoOwningSession())
                    request = offload.OffloadRequest(checkout, "hana", "test", offload.NO_RESULT_FILE, (),
                        "verify", "", target="mac", verb="check", admission=admission)
                    controller = offload.ProcessController()
                    with mock.patch.object(repository, "repository_key", side_effect=AssertionError("Use saved history")), \
                            mock.patch.object(repository, "mac_source_repository", side_effect=AssertionError("Use saved scope")), \
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
                mock.patch.object(mac_queue_run, "admission_command", return_value=["fixture-admission"]), \
                mock.patch.object(mac_queue_run, "capture_command",
                                  return_value=(0, "a\tb\tfixture-mac\td\tepisode\tattempt")), \
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
            with mac_run.held(checkout, "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
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

        with mac_run.held(self.repository("hana", origin=HANA_ORIGIN), "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
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
        with mac_run.held(self.repository("hana", origin=HANA_ORIGIN), "mac", SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
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

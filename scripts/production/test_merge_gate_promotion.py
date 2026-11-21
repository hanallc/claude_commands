"""Merge gate promotion contracts."""

from __future__ import annotations

from scripts.production.staff_test_support import foreign_hostname

import unittest

import json
import io
import os
from pathlib import Path
import subprocess
import sys
from collections.abc import Generator
from types import SimpleNamespace
from typing import cast, final, override
from contextlib import contextmanager, redirect_stdout
from unittest import mock
from scripts.production import change_review, merge_gate, showrunners, staff
from scripts.production.checkpoint_gate import PromotionCiPolicy
from scripts.production.staff_test_support import isolated_staff_file, recorded_showrunner
from scripts.agents import remote
from scripts.production.gate_modules import GateRefused
from scripts.production.merge_gate_test_support import MergeGateFixture, wait_for_file
from scripts.lib import land_main, waiting_rows
from scripts.lib.git_test_support import fixture_git_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.main_push_lock_test_support import publish_after_contender


def setUpModule() -> None:
    unittest.enterModuleContext(foreign_hostname())


def save_fixture_verdict(fixture: MergeGateFixture, owner: staff.StaffRow, tip: str,
                         policy: PromotionCiPolicy = "standard") -> None:
    folder = Path(fixture.environment["SHOWRUNNER_STATE_DIR"]) / owner.id / "session/waiting_state"
    fixture.write(folder / "ci_points.json", json.dumps({"count": 1, "tip": tip, "policy": policy}))


@contextmanager
def green_ci_after_merge(fixture: MergeGateFixture) -> Generator[None]:
    """Supply the CI verdict for tests of the steps after a merge."""
    from scripts.production.unit_lookup import doc_owner

    promote = merge_gate.promote

    def promote_with_verdict(args: merge_gate.GateArguments) -> None:
        record = staff.snapshot()
        doc = change_review.production_doc(args.production, record=record)
        tip = fixture.git(fixture.origin, "rev-parse", f"refs/heads/{args.merge_branch}")
        save_fixture_verdict(fixture, doc_owner(doc, record), tip)
        promote(args)

    with mock.patch.object(merge_gate, "promote", side_effect=promote_with_verdict):
        yield


@final
class MergeGatePromotionTests(MergeGateFixture):
    @override
    def bind_fixture(self, root: Path) -> None:
        super().bind_fixture(root)
        self.environment = {
            **fixture_git_environment(root.parent),
            **{key: value for key, value in self.environment.items() if not key.startswith("GIT_")},
            "GIT_CONFIG_SYSTEM": os.devnull,
            "SHOWRUNNER_STATE_DIR": str(Path(self.environment["HOME"]) / ".local/state/showrunner"),
        }

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment, clear=True)))
        _ = self.enterContext(mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR",
                                                Path(self.environment["SHOWRUNNER_STATE_DIR"])))

    @override
    def run_gate(self, *extra: str, checkpoint: str = "", previous: str = "",
                 ignored_signals: bool = False, callers_production: bool = False,
                 own_process: bool = False, preamble: str = "") -> subprocess.CompletedProcess[str]:
        with green_ci_after_merge(self):
            return super().run_gate(*extra, checkpoint=checkpoint, previous=previous, ignored_signals=ignored_signals,
                                    callers_production=callers_production, own_process=own_process, preamble=preamble)

    @override
    def publish_for_promotion(self) -> merge_gate.GateArguments:
        args = super().publish_for_promotion()
        args.production = "quiet"
        with isolated_staff_file(Path(self.environment["STAFF_FILE"]), self.environment):
            owner = showrunners.owner_for("quiet")
        save_fixture_verdict(self, owner, self.checkpoint)
        return args

    @override
    def review_list(self) -> tuple[Path, Path]:
        review, doc = super().review_list()
        with isolated_staff_file(Path(self.environment["STAFF_FILE"]), self.environment):
            record = staff.snapshot()
            owner = record.by_doc("natedev", doc)
            assert owner is not None
            _ = staff.set_doc(owner.id, staff.LiveDoc(remote.local_machine(), doc))
        return review, doc

    def test_failed_main_push_leaves_rows_unlisted_until_promote_only_succeeds(self) -> None:
        review, _ = self.review_list()
        before = review.read_bytes()
        checkpoint = self.checkpoint_commit("checkpoint(fixture): ready\n\nChange-Review: Published | Read | Ready | image 012345abcdef")
        hook = self.origin / "hooks/pre-receive"
        self.write(hook, '#!/bin/sh\nwhile read old new ref; do\n'
                   + ' if [ "$ref" = refs/heads/main ]; then exit 1; fi\ndone\n')
        hook.chmod(0o755)
        failed = self.run_gate("--promote", "--production", "fixture-showrunner", checkpoint=checkpoint)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertEqual(review.read_bytes(), before)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        published = self.git(self.origin, "rev-parse", "integration")
        hook.unlink()
        retried = self.run_promote_only("--production", "fixture-showrunner")
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), published)
        self.assertEqual([row["Change"] for row in change_review.pending(review)], ["old", "Published"])
        self.assertEqual(change_review.pending(review)[-1]["Merged"].split(" · ")[1], published[:8])
        written = review.read_bytes()
        self.assertEqual(self.run_promote_only("--production", "fixture-showrunner").returncode, 0)
        self.assertEqual(review.read_bytes(), written)

    def test_failed_mac_pull_leaves_rows_unlisted_until_the_next_good_promotion(self) -> None:
        review, _ = self.review_list()
        before = review.read_bytes()
        checkpoint = self.checkpoint_commit("checkpoint(fixture): live\n\nChange-Review: Installed | Read | Live | image 012345abcdef")
        self.write(self.state / "mac-answer", "ff_rc=1\nfixture-head\n")
        failed = self.run_gate("--promote", "--production", "fixture-showrunner", checkpoint=checkpoint)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertEqual(review.read_bytes(), before)
        published = self.git(self.origin, "rev-parse", "integration")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), published)
        self.write(self.state / "mac-answer", "ff_rc=0\nfixture-head\n")
        retried = self.run_promote_only("--production", "fixture-showrunner")
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual([row["Change"] for row in change_review.pending(review)], ["old", "Installed"])
        self.assertEqual(change_review.pending(review)[-1]["Merged"].split(" · ")[1], published[:8])

    def test_remote_tracked_check_runs_without_repository_modules(self) -> None:
        self.write(self.unit / "subject.txt", "promoted contents\n")
        self.checkpoint = self.commit(self.unit, "update subject")
        empty = self.root / "isolated-python"
        empty.mkdir()
        index_path = Path(self.git(self.main, "rev-parse", "--git-path", "index"))
        index = index_path.read_bytes()
        isolation = """import sys
class NoRepositoryModules:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "scripts" or fullname.startswith("scripts."):
            raise ModuleNotFoundError("remote host has no repository modules")
sys.meta_path.insert(0, NoRepositoryModules())
"""
        environment = {**self.environment, "GIT_DIR": self.git(self.main, "rev-parse", "--absolute-git-dir"),
                       "GIT_WORK_TREE": str(self.main)}
        for edited in (False, True):
            with self.subTest(edited=edited):
                if edited:
                    self.write(self.main / "subject.txt", "remote local edit\n")
                result = subprocess.run([sys.executable, "-I", "-c",
                                         isolation + merge_gate.PROMOTION_TRACKED_CHECK, self.checkpoint],
                                        cwd=empty, env=environment, text=True,
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, int(edited), result.stdout + result.stderr)
                self.assertEqual(result.stderr, "")
                if edited:
                    self.assertIn('promotion refused: tracked edits overlap promotion: "subject.txt"', result.stdout)
                else:
                    self.assertEqual(result.stdout, "")
                self.assertEqual(index_path.read_bytes(), index)

    def test_mac_timeout_or_interruption_reports_that_main_was_already_promoted(self) -> None:
        args = merge_gate.GateArguments(main_repo=str(self.main), merge_branch="integration",
                                       production="quiet", ssh="fake-ssh", mac_repo="~/fixture", mac_host="fixture")
        with isolated_staff_file(Path(self.environment["STAFF_FILE"]), self.environment):
            owner = showrunners.owner_for("quiet")
        save_fixture_verdict(self, owner, self.base)
        for failure in (subprocess.TimeoutExpired("fake-ssh", 25), GateRefused("interrupted by SIGTERM"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), mock.patch.dict(os.environ, self.environment), mock.patch.object(merge_gate, "wait_for_release_lock"), mock.patch.object(
                    merge_gate, "checked_git", side_effect=["main", "", self.base, self.base, self.base, self.base]), mock.patch.object(
                    land_main, "land", return_value=land_main.Landed(
                        self.base, "main", land_main.Pushed(), land_main.HookAbsent())) as landed, mock.patch.object(
                    waiting_rows, "move_to"), mock.patch.object(
                    merge_gate, "git", return_value=subprocess.CompletedProcess([], 0, "", "")), mock.patch.object(
                    merge_gate, "promotion_edit_conflict", return_value=""), mock.patch.object(
                    merge_gate, "live_state_refusal", return_value=""), mock.patch.object(
                    merge_gate, "subprocess", SimpleNamespace(run=mock.Mock(side_effect=failure),
                                                            DEVNULL=subprocess.DEVNULL,
                                                            SubprocessError=subprocess.SubprocessError)):
                with self.assertRaisesRegex(GateRefused, f"main promoted to {self.base}; Mac fast-forward failed"):
                    merge_gate.promote(args)
            landed.assert_called_once_with(self.main, self.base)

    def test_promotion_retries_its_recorded_fast_forward_after_push_failure(self) -> None:
        for route in ("legacy", "enrolled", "enroll-after-failure"):
            with self.subTest(route=route):
                if route != "legacy":
                    self.restore_fixture(self.suites)
                self.check_promotion_retry(route)

    def check_promotion_retry(self, route: str) -> None:
        _ = super().publish_for_promotion()
        _ = self.git(self.main, "switch", "--detach", "main")
        main = self.checkout
        _ = self.git(main, "switch", "main")
        integration = self.root / "integration"
        _ = self.git(main, "worktree", "add", str(integration), "integration")
        doc = main / "docs/plans/cargo-liner-production-live.md"
        legacy = f"- **Showrunner checkout:** `{main}`\n- **Merge branch:** `integration`\n- **Log:** `production.log`\n"
        enrolled = legacy.replace(str(main), str(integration)) + f"- **Merge checkout:** `{main}`\n"
        self.write(doc, enrolled if route == "enrolled" else legacy)
        with isolated_staff_file(Path(self.environment["STAFF_FILE"]), self.environment):
            owner = recorded_showrunner("cargo-liner", doc)
        save_fixture_verdict(self, owner, self.checkpoint, "cargo-liner" if route == "enrolled" else "standard")
        options = ("--production", "cargo-liner", "--main-repo", str(main))

        hook = self.origin / "hooks/pre-receive"
        self.write(hook, '#!/bin/sh\nwhile read old new ref; do\n'
                   + '  if [ "$ref" = refs/heads/main ]; then echo "injected push failure" >&2; exit 1; fi\n'
                   + "done\n")
        hook.chmod(0o755)
        stopped = self.run_promote_only(*options)
        self.assertNotEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("injected push failure", stopped.stderr)
        self.assertEqual(self.git(main, "rev-parse", "HEAD"), self.checkpoint)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        hook.unlink()
        if route == "enroll-after-failure":
            self.write(doc, enrolled)

        self.write(integration / "next.txt", "next candidate\n")
        latest = self.commit(integration, "new integration candidate")
        _ = self.git(integration, "push", "origin", "integration")
        held = self.run_promote_only(*options)
        self.assertNotEqual(held.returncode, 0, held.stdout + held.stderr)
        self.assertIn("collect cargo-liner CI on", held.stderr)
        self.assertIn(latest, held.stderr)
        self.assertEqual(self.git(main, "rev-parse", "HEAD"), self.checkpoint)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

        save_fixture_verdict(self, owner, latest, "standard" if route == "legacy" else "cargo-liner")
        result = self.run_promote_only(*options)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(main, "rev-parse", "HEAD"), latest)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), latest)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 1)

    def test_promotion_rejects_an_unrecorded_commit_with_the_same_tree(self) -> None:
        args = self.publish_for_promotion()
        with mock.patch.dict(os.environ, self.environment), mock.patch.object(
                land_main, "land", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                merge_gate.promote(args)
        _ = self.git(self.main, "commit", "--allow-empty", "-m", "unrelated local commit")
        local = self.git(self.main, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD^{tree}"),
                         self.git(self.main, "rev-parse", self.checkpoint + "^{tree}"))
        result = self.run_promote_only()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), local)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    def test_promote_fast_forwards_scratch_main_and_checks_mac_result(self) -> None:
        result = self.run_gate("--promote")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        head = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), head)
        ssh_calls = [call for call in self.calls() if call["tool"] == "ssh"]
        self.assertEqual(len(ssh_calls), 1)
        self.assertIn("--ff-only", " ".join(ssh_calls[0]["args"]))
        self.assertIn("ff_rc=", " ".join(ssh_calls[0]["args"]))

        with self.subTest(holder="another process"):
            _ = self.git(self.unit, "fetch", "origin", "integration")
            _ = self.git(self.unit, "merge", "--ff-only", "origin/integration")
            self.write(self.unit / "second.txt", "another promotion\n")
            self.checkpoint = self.commit(self.unit, "another promotion")
            _ = self.publish_for_promotion()
            command = [sys.executable, str(Path(merge_gate.__file__)), "--repo", str(self.checkout),
                       "--merge-branch", "integration", "--promote-only", "--main-repo", str(self.main),
                       "--ssh", str(self.bin / "ssh"), "--production", "quiet"]
            _ = publish_after_contender(self, self.main, self.origin, command, self.environment, cwd=self.root)
            self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)

    def test_promote_only_promotes_the_pushed_merge_branch_without_a_gate(self) -> None:
        self.assertEqual(self.run_gate("--promote").returncode, 0)
        result = self.run_promote_only()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.git(self.origin, "rev-parse", "main"))
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 2)

    def test_finished_release_and_live_command_text_do_not_block_promotion(self) -> None:
        args = self.publish_for_promotion()
        release = Path(self.environment["BUILD_HOLD_RELEASE_DIR"])
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        cycle_id = "a" * 32
        instant = "2026-10-28T14:50:00-04:00"
        self.write(release / cycle_id / "cycle.json", json.dumps({
            "id": cycle_id, "opened_at": instant, "release_started_at": instant,
            "holders": {"fixture": {"since": instant, "released_at": instant}},
            "recipients": {}, "entries": []}))
        self.write(release / "current", cycle_id)
        self.write(release / "release.lock", "")
        ready = self.state / "command-text-ready"
        source = ("import pathlib, signal, sys; signal.signal(signal.SIGTERM, signal.SIG_DFL); "
                  "pathlib.Path(sys.argv[1]).touch(); sys.stdin.readline()")
        process = subprocess.Popen(
            [sys.executable, "-c", source, str(ready), "build_hold.py", "release"],
            env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True)
        try:
            wait_for_file(ready)
            with mock.patch.dict(os.environ, self.environment), redirect_stdout(io.StringIO()):
                merge_gate.promote(args)
            self.assertIsNone(process.poll())
            self.assertEqual(list(holders.iterdir()), [])
            self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
            self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)
        finally:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_promotion_waits_for_another_process_to_free_the_release_lock(self) -> None:
        args = self.publish_for_promotion()
        with self.release_lock_held_by_child() as process:
            def release_during_wait(_seconds: float) -> None:
                self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
                self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))
                _, errors = process.communicate(input="release\n", timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 0, errors)

            pause = mock.Mock(side_effect=release_during_wait)
            clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[10.0, 10.0, 11.0]), sleep=pause)
            with mock.patch.dict(os.environ, self.environment), \
                    mock.patch.object(merge_gate, "time", clock), redirect_stdout(io.StringIO()):
                merge_gate.promote(args, release_lock_wait_s=0.5, release_lock_poll_s=0.01)
            pause.assert_called_once_with(0.01)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)

    def test_promotion_refuses_when_the_release_lock_stays_held_for_the_whole_wait(self) -> None:
        args = self.publish_for_promotion()
        pause = mock.Mock()
        clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[10.0, 10.0, 70.0]), sleep=pause)
        with self.release_lock_held_by_child(), mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "time", clock):
            with self.assertRaisesRegex(GateRefused,
                    "^build-hold state stayed locked for 60 s; main promotion refused$"):
                merge_gate.promote(args)
            pause.assert_called_once_with(0.2)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promotion_does_not_create_a_missing_release_directory_or_lock(self) -> None:
        args = self.publish_for_promotion()
        release = Path(self.environment["BUILD_HOLD_RELEASE_DIR"])
        with mock.patch.dict(os.environ, self.environment), redirect_stdout(io.StringIO()):
            merge_gate.promote(args)
            self.assertFalse(release.exists())
            release.mkdir()
            merge_gate.promote(args)
            self.assertEqual(list(release.iterdir()), [])
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 2)

    def test_promote_allows_unrelated_local_edits_and_keeps_untracked_files(self) -> None:
        _ = self.publish_for_promotion()
        self.write(self.main / "subject.txt", "staged main\n")
        _ = self.git(self.main, "add", "subject.txt")
        self.write(self.main / "subject.txt", "uncommitted main\n")
        self.write(self.main / "settings.json", "local settings\n")
        self.write(self.main / "accounts.lock", "untracked\n")
        result = self.run_promote_only()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.main / "subject.txt").read_text(), "uncommitted main\n")
        self.assertEqual(self.git(self.main, "show", ":subject.txt"), "staged main")
        self.assertEqual((self.main / "settings.json").read_text(), "local settings\n")
        self.assertEqual((self.main / "accounts.lock").read_text(), "untracked\n")
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)

    def test_promotion_refuses_staged_edits_even_when_the_file_matches_head(self) -> None:
        self.write(self.unit / "subject.txt", "promoted contents\n")
        self.checkpoint = self.commit(self.unit, "update subject")
        _ = self.publish_for_promotion()
        self.write(self.main / "subject.txt", "staged main\n")
        _ = self.git(self.main, "add", "subject.txt")
        self.write(self.main / "subject.txt", "base\n")
        result = self.run_promote_only()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"subject.txt"', result.stderr)
        self.assertEqual(self.git(self.main, "show", ":subject.txt"), "staged main")
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    def test_promotion_refuses_a_tree_that_cannot_read_live_state(self) -> None:
        self.write(self.unit / "config/merge_gate.conf",
                   "# text:\nlive_state_checks=state_reader\n# whole: 1..\nlive_state_check_seconds=60\n")
        self.write(self.unit / "state_reader.py",
                   "import sys\nprint('stored rows need conversion', file=sys.stderr)\nsys.exit(2)\n")
        self.checkpoint = self.commit(self.unit, "add a state reader that refuses")
        _ = self.publish_for_promotion()
        result = self.run_promote_only()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cannot read live state: state_reader: stderr: stored rows need conversion; run its conversion",
                      result.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    def test_promotion_runs_the_target_tree_state_check(self) -> None:
        self.write(self.unit / "config/merge_gate.conf",
                   "# text:\nlive_state_checks=state_reader\n# whole: 1..\nlive_state_check_seconds=60\n")
        self.write(self.unit / "state_reader.py", "import sys\nassert sys.argv[1:] == ['state-check']\n")
        self.checkpoint = self.commit(self.unit, "add a state reader that reads")
        _ = self.publish_for_promotion()
        result = self.run_promote_only()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)

    def test_promotion_refuses_a_listed_state_check_absent_from_the_tree(self) -> None:
        self.write(self.unit / "config/merge_gate.conf",
                   "# text:\nlive_state_checks=state_reader absent_reader\n# whole: 1..\nlive_state_check_seconds=60\n")
        self.write(self.unit / "state_reader.py", "import sys\nassert sys.argv[1:] == ['state-check']\n")
        self.checkpoint = self.commit(self.unit, "list a state reader the tree does not have")
        _ = self.publish_for_promotion()
        result = self.run_promote_only()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("promotion refused: live_state_checks names absent_reader, absent from ", result.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    def test_promotion_refuses_edits_hidden_by_index_flags(self) -> None:
        self.write(self.unit / "subject.txt", "promoted contents\n")
        self.checkpoint = self.commit(self.unit, "update subject")
        _ = self.publish_for_promotion()
        for flag in ("assume-unchanged", "skip-worktree"):
            with self.subTest(flag=flag):
                _ = self.git(self.main, "update-index", "--" + flag, "subject.txt")
                self.write(self.main / "subject.txt", "live contents\n")
                self.assertEqual(self.git(self.main, "status", "--porcelain", "--untracked-files=no"), "")
                result = self.run_promote_only()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('"subject.txt"', result.stderr)
                self.assertEqual((self.main / "subject.txt").read_text(), "live contents\n")
                self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
                self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
                _ = self.git(self.main, "update-index", "--no-" + flag, "subject.txt")

    def test_promotion_checks_edits_after_the_last_hold_wait(self) -> None:
        self.write(self.unit / "subject.txt", "promoted contents\n")
        self.checkpoint = self.commit(self.unit, "update subject")
        args = self.publish_for_promotion()
        waits = 0

        def edit_during_wait() -> None:
            nonlocal waits
            waits += 1
            if waits == 2:
                self.write(self.main / "subject.txt", "edited during hold\n")

        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate.BuildHoldWait, "wait_until_free", side_effect=edit_during_wait):
            with self.assertRaisesRegex(GateRefused, 'promotion refused: tracked edits overlap promotion: "subject.txt"'):
                merge_gate.promote(args)
        self.assertEqual((self.main / "subject.txt").read_text(), "edited during hold\n")
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promotion_allows_untracked_files(self) -> None:
        _ = self.publish_for_promotion()
        self.write(self.main / "accounts.lock", "untracked\n")
        result = self.run_promote_only()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.main / "accounts.lock").read_text(), "untracked\n")
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)

    def test_mac_promotion_checks_real_files_and_index_in_a_private_clone(self) -> None:
        self.write(self.unit / "subject.txt", "promoted contents\n")
        self.checkpoint = self.commit(self.unit, "update subject")
        args = self.publish_for_promotion()
        self.write(self.bin / "ssh", f'#!{sys.executable}\nimport subprocess, sys\n'
                   + 'raise SystemExit(subprocess.call(["sh", "-c", sys.argv[-1]]))\n')
        for state in ("modified", "staged", "assume-unchanged", "skip-worktree", "unrelated", "untracked", "clean"):
            with self.subTest(state=state):
                mac = self.root / ("mac-" + state)
                _ = self.git(self.root, "clone", "-q", "--branch", "main", str(self.origin), str(mac))
                # Keep each Mac at the old promotion, even after natedev advances.
                _ = self.git(mac, "reset", "--hard", self.base)
                args.mac_repo = str(mac)
                if state in ("assume-unchanged", "skip-worktree"):
                    _ = self.git(mac, "update-index", "--" + state, "subject.txt")
                if state == "unrelated":
                    self.write(mac / "settings.json", "local settings\n")
                    _ = self.git(mac, "add", "settings.json")
                    self.write(mac / "settings.json", "more local settings\n")
                elif state not in ("untracked", "clean"):
                    self.write(mac / "subject.txt", "Mac local contents\n")
                if state == "staged":
                    _ = self.git(mac, "add", "subject.txt")
                if state == "untracked":
                    self.write(mac / "local.lock", "keep\n")
                with mock.patch.dict(os.environ, self.environment), redirect_stdout(io.StringIO()):
                    if state in ("unrelated", "untracked", "clean"):
                        merge_gate.promote(args)
                        self.assertEqual(self.git(mac, "rev-parse", "HEAD"), self.checkpoint)
                    else:
                        with self.assertRaisesRegex(GateRefused, 'Mac fast-forward failed:.*"subject.txt"'):
                            merge_gate.promote(args)
                        self.assertEqual(self.git(mac, "rev-parse", "HEAD"), self.base)
                        self.assertEqual((mac / "subject.txt").read_text(), "Mac local contents\n")
                self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)
                if state == "unrelated":
                    self.assertEqual((mac / "settings.json").read_text(), "more local settings\n")
                    self.assertEqual(self.git(mac, "show", ":settings.json"), "local settings")
                if state == "untracked":
                    self.assertEqual((mac / "local.lock").read_text(), "keep\n")

    def test_promote_preserves_local_edits_that_block_fast_forward(self) -> None:
        self.write(self.unit / "subject.txt", "checkpoint changes this file\n")
        self.checkpoint = self.commit(self.unit, "touch subject")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        self.write(self.main / "subject.txt", "uncommitted main\n")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.main / "subject.txt").read_text(), "uncommitted main\n")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promote_refuses_divergent_main(self) -> None:
        self.write(self.main / "main-only.txt", "divergent\n")
        main_head = self.commit(self.main, "main divergence")
        _ = self.git(self.main, "push", "origin", "main")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), main_head)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), main_head)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promote_refuses_unpublished_main_commits(self) -> None:
        self.write(self.main / "main-only.txt", "unpublished\n")
        main_head = self.commit(self.main, "unpublished main")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), main_head)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promote_refuses_checkout_on_another_branch(self) -> None:
        _ = self.git(self.main, "checkout", "-b", "other-main")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.main, "branch", "--show-current"), "other-main")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_mac_nonzero_ff_rc_is_failure_even_when_ssh_exits_zero(self) -> None:
        self.write(self.state / "mac-answer", "ff_rc=1\nfixture-head\n")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ff_rc=1", result.stdout + result.stderr)
        head = self.git(self.origin, "rev-parse", "main")
        self.assertIn(f"main promoted to {head}; Mac fast-forward failed", result.stderr)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 1)

    def test_mac_missing_ff_rc_is_failure(self) -> None:
        self.write(self.state / "mac-answer", "fixture-head\n")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 1)

    def test_mac_conflicting_ff_rc_lines_are_failure(self) -> None:
        self.write(self.state / "mac-answer", "ff_rc=0\nff_rc=1\nfixture-head\n")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 1)


if __name__ == "__main__":
    _ = unittest.main()

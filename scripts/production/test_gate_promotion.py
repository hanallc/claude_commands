"""Gate promotion contracts."""

from __future__ import annotations

from scripts.production.staff_test_support import foreign_hostname

import ast
import unittest

import json
import io
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import final, override
from contextlib import redirect_stdout
from unittest import mock
from scripts.production import change_review, gate, promotion, showrunners
from scripts.production.staff_test_support import isolated_staff_file, recorded_showrunner
from scripts.production.gate_modules import GateRefused
from scripts.production.gate_test_support import MergeGateFixture, wait_for_file
from scripts.lib import land_main, waiting_rows
from scripts.lib.git_test_support import fixture_git_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.main_push_lock_test_support import publish_after_contender


def setUpModule() -> None:
    unittest.enterModuleContext(foreign_hostname())


class ProductionPublicationBoundaryTests(unittest.TestCase):
    def test_production_main_publication_uses_the_promotion_operation(self) -> None:
        integration_publishers = {"ci_points.py", "merge_checkpoint.py", "gate.py", "production_lifecycle.py"}
        legacy_publishers = {
            "merge_checkpoint.py": {"push_git"},
            "gate.py": {"merge_checkpoint"},
            "production_lifecycle.py": {"open_production", "wrap"},
        }
        for path in sorted(Path(__file__).parent.glob("*.py")):
            if path.name.startswith("test_") or path.name.endswith("_test_support.py") or path.name == "promotion.py":
                continue
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                direct_calls: list[int] = []
                legacy_pushes = {child.lineno for node in ast.walk(tree)
                                 if isinstance(node, ast.FunctionDef) and node.name in legacy_publishers.get(path.name, set())
                                 for child in ast.walk(node) if isinstance(child, ast.Call)}
                land_modules = {"land_main"}
                land_names: set[str] = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        if node.module == "scripts.lib":
                            land_modules.update(alias.asname or alias.name for alias in node.names
                                                if alias.name == "land_main")
                        if node.module == "scripts.lib.land_main":
                            land_names.update(alias.asname or alias.name for alias in node.names if alias.name == "land")
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        function = node.func
                        primitive = (isinstance(function, ast.Name) and function.id in land_names
                                     or isinstance(function, ast.Attribute) and function.attr == "land"
                                     and isinstance(function.value, ast.Name) and function.value.id in land_modules)
                        if primitive and path.name != "fast_track.py":
                            direct_calls.append(node.lineno)
                        if isinstance(function, (ast.Name, ast.Attribute)):
                            name = function.id if isinstance(function, ast.Name) else function.attr
                            if (name in {"ApprovedPromotion", "PublishedMain"}
                                    or name == "FastTrackInstall" and path.name != "fast_track.py"):
                                direct_calls.append(node.lineno)
                        arguments = node.args
                    elif isinstance(node, (ast.List, ast.Tuple)):
                        arguments = node.elts
                    else:
                        continue
                    for index, argument in enumerate(arguments):
                        if not isinstance(argument, ast.Constant) or argument.value != "push":
                            continue
                        refspecs = [ast.unparse(item) for item in arguments[index + 1:]]
                        if not refspecs or refspecs[0] in ("'fetch'", "'ls-remote'", "'rev-parse'", "'push'"):
                            continue
                        if "'--delete'" in refspecs:
                            continue
                        if node.lineno in legacy_pushes and refspecs[-2:] == ["'origin'", "'main'"]:
                            continue
                        main_ref = any(item in ("'main'", "'refs/heads/main'") or item.endswith(":main'")
                                       or item.endswith(":refs/heads/main'") for item in refspecs)
                        if main_ref or (path.name in integration_publishers
                                        and refspecs[0] in ("'origin'", "'-u'", "'--set-upstream'")):
                            direct_calls.append(node.lineno)
                self.assertEqual(direct_calls, [], "Use promotion for production pushes")

        for module in ("ci_points.py", "merge_checkpoint.py"):
            with self.subTest(shell_caller=module):
                source = Path(__file__).with_name(module).read_text(encoding="utf-8")
                self.assertIn("PRODUCTION_INTEGRATION_PUSH", source)
                self.assertIn("promotion.integration_push_script(production)", source)


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
        _ = self.enterContext(mock.patch.object(
            showrunners, "SHOWRUNNER_STATE_DIR", Path(self.environment["SHOWRUNNER_STATE_DIR"])))

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
                                         isolation + gate.PROMOTION_TRACKED_CHECK, self.checkpoint],
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
        args = self.publish_for_promotion()
        promote_mac = gate.promote_mac
        for failure in (subprocess.TimeoutExpired("fake-ssh", 25), GateRefused("interrupted by SIGTERM"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), mock.patch.dict(os.environ, self.environment):
                def fail_mac(options: gate.GateArguments, main: Path, published: promotion.PublishedMain) -> None:
                    with mock.patch.object(gate, "subprocess", SimpleNamespace(
                            run=mock.Mock(side_effect=failure), DEVNULL=subprocess.DEVNULL,
                            SubprocessError=subprocess.SubprocessError)):
                        promote_mac(options, main, published)

                with mock.patch.object(gate, "promote_mac", side_effect=fail_mac):
                    with self.assertRaisesRegex(
                            GateRefused, f"main promoted to {self.checkpoint}; Mac fast-forward failed"):
                        gate.promote(args)
                self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)

    def test_promotion_retries_its_recorded_fast_forward_after_push_failure(self) -> None:
        args = self.publish_for_promotion()
        with mock.patch.dict(os.environ, self.environment), mock.patch.object(
                land_main, "land", side_effect=land_main.LandError("injected push failure")):
            with self.assertRaisesRegex(GateRefused, "injected push failure"):
                gate.promote(args)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        result = self.run_promote_only()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 1)

    def test_promotion_refuses_missing_or_stale_ci_for_the_exact_tip(self) -> None:
        _ = self.publish_for_promotion()
        receipt = self.save_ci_receipt(self.checkpoint)
        receipt.unlink()
        for state in ("missing", "stale"):
            with self.subTest(receipt=state):
                if state == "stale":
                    _ = self.save_ci_receipt(self.base)
                result = self.run_promote_only()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"collect quiet CI on {self.checkpoint}", result.stderr)
                self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
                self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
                self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))
        _ = self.save_ci_receipt(self.checkpoint)
        result = self.run_promote_only()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)

    def enrolled_fixture(self, *, enrolled: bool = True) -> tuple[Path, Path, str]:
        _ = self.git(self.main, "switch", "--detach", "main")
        _ = self.git(self.checkout, "switch", "main")
        integration = self.root / "integration"
        _ = self.git(self.checkout, "worktree", "add", str(integration), "integration")
        doc = self.checkout / "docs/plans/cargo-liner-production-live.md"
        legacy = (f"- **Showrunner checkout:** `{self.checkout}`\n"
                  + "- **Merge branch:** `integration`\n- **Log:** `production.log`\n")
        declared = legacy.replace(str(self.checkout), str(integration)) + f"- **Merge checkout:** `{self.checkout}`\n"
        self.write(doc, declared if enrolled else legacy)
        with isolated_staff_file(Path(self.environment["STAFF_FILE"]), self.environment):
            _ = recorded_showrunner("cargo-liner", doc)
        return integration, doc, declared

    def test_promotion_retries_after_enrollment_and_requires_ci_for_the_new_tip(self) -> None:
        for enrolled in (True, False):
            with self.subTest(enrolled_at_first_push=enrolled):
                if not enrolled:
                    self.restore_fixture(self.suites)
                _ = self.publish_for_promotion()
                integration, doc, declared = self.enrolled_fixture(enrolled=enrolled)
                _ = self.save_ci_receipt(self.checkpoint, production="cargo-liner",
                                         policy="cargo-liner" if enrolled else "standard")
                options = ("--production", "cargo-liner", "--main-repo",
                           str(self.main if enrolled else self.checkout))
                hook = self.origin / "hooks/pre-receive"
                self.write(hook, '#!/bin/sh\nwhile read old new ref; do\n'
                           + '  if [ "$ref" = refs/heads/main ]; then echo "injected push failure" >&2; exit 1; fi\n'
                           + "done\n")
                hook.chmod(0o755)
                stopped = self.run_promote_only(*options)
                self.assertIn("injected push failure", stopped.stderr)
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.checkpoint)
                self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
                hook.unlink()
                self.write(doc, declared)

                self.write(integration / "next.txt", "next candidate\n")
                latest = self.commit(integration, "new integration candidate")
                _ = self.git(integration, "push", "origin", "integration")
                held = self.run_promote_only(*options)
                self.assertNotEqual(held.returncode, 0, held.stdout + held.stderr)
                self.assertIn(f"collect cargo-liner CI on the integration tip before promoting {latest}", held.stderr)
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.checkpoint)
                self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

                _ = self.save_ci_receipt(latest, production="cargo-liner", policy="cargo-liner")
                result = self.run_promote_only(*options)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), latest)
                self.assertEqual(self.git(self.origin, "rev-parse", "main"), latest)
                self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 1)

    def test_checkpoint_and_promote_only_refuse_a_branch_other_than_the_production_branch(self) -> None:
        for mode in ("checkpoint", "promote-only"):
            with self.subTest(mode=mode):
                options = ("--merge-branch", "main")
                result = self.run_gate(*options) if mode == "checkpoint" else self.run_promote_only(*options)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"{self.quiet_production}: expected merge branch integration", result.stderr)
                self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
                self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
                self.assertEqual(self.calls(), [])

    def test_a_legacy_production_can_publish_its_main_checkpoint_without_ci(self) -> None:
        _ = self.git(self.main, "switch", "--detach", "main")
        _ = self.git(self.checkout, "switch", "main")
        self.write(self.quiet_production, "# quiet\n\n- **Merge branch:** `main`\n")
        result = self.run_gate("--promote", "--merge-branch", "main", "--main-repo", str(self.checkout))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tip = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), tip)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD^2"), self.checkpoint)

    def test_enrolled_gate_refuses_incoming_live_files_in_both_publication_modes(self) -> None:
        integration, doc, declared = self.enrolled_fixture()
        relative_doc = doc.relative_to(self.checkout)
        self.write(self.unit / relative_doc, "tracked copy\n")
        checkpoint = self.commit(self.unit, "track a live production document")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        options = ("--production", "cargo-liner")
        result = self.run_gate(*options, checkpoint=checkpoint)
        self.assert_refused_before_gate(result)
        self.assertIn(str(doc), result.stderr)
        self.assertEqual(self.git(integration, "rev-parse", "HEAD"), self.base)

        _ = self.git(integration, "merge", "--ff-only", checkpoint)
        _ = self.git(integration, "push", "origin", "integration")
        _ = self.save_ci_receipt(checkpoint, production="cargo-liner", policy="cargo-liner")
        result = self.run_promote_only(*options)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("incoming tree conflicts with live production files", result.stderr)
        self.assertIn(str(doc), result.stderr)
        self.assertEqual(doc.read_text(encoding="utf-8"), declared)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    def test_promotion_rejects_an_unrecorded_commit_with_the_same_tree(self) -> None:
        args = self.publish_for_promotion()
        with mock.patch.dict(os.environ, self.environment), mock.patch.object(
                land_main, "land", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                gate.promote(args)
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
            command = [sys.executable, str(Path(gate.__file__)), "--repo", str(self.checkout),
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
                gate.promote(args)
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
                    mock.patch.object(gate, "time", clock), redirect_stdout(io.StringIO()):
                gate.promote(args, release_lock_wait_s=0.5, release_lock_poll_s=0.01)
            pause.assert_called_once_with(0.01)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)

    def test_promotion_refuses_when_the_release_lock_stays_held_for_the_whole_wait(self) -> None:
        args = self.publish_for_promotion()
        pause = mock.Mock()
        clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[10.0, 10.0, 70.0]), sleep=pause)
        with self.release_lock_held_by_child(), mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(gate, "time", clock):
            with self.assertRaisesRegex(GateRefused,
                    "^build-hold state stayed locked for 60 s; main promotion refused$"):
                gate.promote(args)
            pause.assert_called_once_with(0.2)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promotion_does_not_create_a_missing_release_directory_or_lock(self) -> None:
        args = self.publish_for_promotion()
        release = Path(self.environment["BUILD_HOLD_RELEASE_DIR"])
        with mock.patch.dict(os.environ, self.environment), redirect_stdout(io.StringIO()):
            gate.promote(args)
            self.assertFalse(release.exists())
            release.mkdir()
            gate.promote(args)
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
        self.write(self.unit / "config/gate.conf",
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
        with self.subTest(config="merge_gate.conf"):
            _ = (self.unit / "config/gate.conf").rename(self.unit / "config/merge_gate.conf")
            older = self.commit(self.unit, "use the old gate config path")
            self.assertIn("cannot read live state: state_reader: stderr: stored rows need conversion; run its conversion",
                          gate.live_state_refusal(self.unit, older))

    def test_promotion_runs_the_target_tree_state_check(self) -> None:
        self.write(self.unit / "config/gate.conf",
                   "# text:\nlive_state_checks=state_reader\n# whole: 1..\nlive_state_check_seconds=60\n")
        self.write(self.unit / "state_reader.py", "import sys\nassert sys.argv[1:] == ['state-check']\n")
        self.checkpoint = self.commit(self.unit, "add a state reader that reads")
        _ = self.publish_for_promotion()
        result = self.run_promote_only()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)

    def test_promotion_refuses_a_listed_state_check_absent_from_the_tree(self) -> None:
        self.write(self.unit / "config/gate.conf",
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
                mock.patch.object(gate.BuildHoldWait, "wait_until_free", side_effect=edit_during_wait):
            with self.assertRaisesRegex(GateRefused, 'promotion refused: tracked edits overlap promotion: "subject.txt"'):
                gate.promote(args)
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
                        gate.promote(args)
                        self.assertEqual(self.git(mac, "rev-parse", "HEAD"), self.checkpoint)
                    else:
                        with self.assertRaisesRegex(GateRefused, 'Mac fast-forward failed:.*"subject.txt"'):
                            gate.promote(args)
                        self.assertEqual(self.git(mac, "rev-parse", "HEAD"), self.base)
                        self.assertEqual((mac / "subject.txt").read_text(), "Mac local contents\n")
                self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)
                if state == "unrelated":
                    self.assertEqual((mac / "settings.json").read_text(), "more local settings\n")
                    self.assertEqual(self.git(mac, "show", ":settings.json"), "local settings")
                if state == "untracked":
                    self.assertEqual((mac / "local.lock").read_text(), "keep\n")

    def test_mac_retry_recovers_a_settings_move_after_head_advanced(self) -> None:
        args = self.publish_for_promotion()
        mac = self.root / "mac-retry"
        _ = self.git(self.root, "clone", "-q", "--branch", "integration", str(self.origin), str(mac))
        args.mac_repo = str(mac)
        relative = "config/retry.conf"
        file = mac / relative
        self.write(file, "limit=5\n")
        head = self.commit(mac, "Mac settings update")
        plan = waiting_rows.MovePlan(
            source=self.checkpoint, target=head,
            before={relative: "limit=5\n"}, after={relative: "limit=6\n"},
            writes=waiting_rows.MoveWrites(
                index_before=self.git(mac, "rev-parse", f"{self.checkpoint}^{{tree}}"),
                expected_index=self.git(mac, "rev-parse", "HEAD^{tree}"),
                files_before={relative: waiting_rows.file_version(file)}))
        receipt = mac / ".git/waiting-rows.move"
        self.write(receipt, json.dumps(plan))
        self.write(self.bin / "ssh", f'#!{sys.executable}\nimport subprocess, sys\n'
                   + 'raise SystemExit(subprocess.call(["sh", "-c", sys.argv[-1]]))\n')
        self.write(mac / "scripts/lib/py", '#!/bin/sh\nshift\n'
                   + 'exec "$GATE_TEST_REPO/scripts/lib/py" "$GATE_TEST_REPO/scripts/lib/waiting_rows.py" "$@"\n')
        (mac / "scripts/lib/py").chmod(0o755)
        self.write(mac / "scripts/lib/waiting_rows.py", "")

        with mock.patch.dict(os.environ, self.environment), redirect_stdout(io.StringIO()):
            gate.promote(args)

        self.assertFalse(receipt.exists())
        self.assertEqual(file.read_text(), "limit=6\n")
        self.assertEqual(self.git(mac, "rev-parse", "HEAD"), head)

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

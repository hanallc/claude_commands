"""Land commits and retry hooks against a private local origin."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from typing import cast, override
from unittest import mock

from scripts.lib import land_main, main_push_lock, unit_runs
from scripts.lib.rerun_test_support import SCRIPTS, ScriptRepositoryTestCase
from scripts.production import behavior_check


class LandMainTests(ScriptRepositoryTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.environment.update(TEST_HOOK_LOG=str(self.root / "hook-runs"),
                                TEST_HOOK_FAIL=str(self.root / "hook-fails"),
                                SHOWRUNNER_STATE_DIR=str(self.root / "showrunner"))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment, clear=True)))

    def next_commit(self, text: str = "next\n") -> str:
        _ = self.write(self.repo / "seed.txt", text)
        _ = self.git("commit", "-am", text.strip())
        return self.git("rev-parse", "HEAD").stdout.strip()

    def install_hook(self) -> None:
        _ = self.write(self.repo / ".claude/config/post_push.sh", '''#!/bin/bash
set -eu
printf '%s|%s|%s\n' "$PWD" "$LANDED_SHA" "$LANDED_BRANCH" >> "$TEST_HOOK_LOG"
if [ -f "$TEST_HOOK_FAIL" ]; then exit 9; fi
''')

    def hook_runs(self) -> list[str]:
        path = Path(self.environment["TEST_HOOK_LOG"])
        return path.read_text().splitlines() if path.exists() else []

    def completion(self) -> Path:
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        return Path(common) / "post-push-completed"

    def test_fetches_the_symbolic_remote_head_without_updating_local_refs(self) -> None:
        writer = self.root / "writer"
        _ = self.git("clone", str(self.remote), str(writer))
        _ = self.git("-C", str(writer), "switch", "-c", "trunk", "origin/main")
        _ = self.git("-C", str(writer), "commit", "--allow-empty", "-m", "Publish trunk.")
        commit = self.git("-C", str(writer), "rev-parse", "HEAD").stdout.strip()
        _ = self.git("-C", str(writer), "push", "origin", "trunk")
        _ = self.git("-C", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/trunk")
        old_head = self.run_in_repo("git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if old_head.returncode == 0:
            _ = self.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        fetch_head = self.write(self.repo / ".git/FETCH_HEAD", "saved fetch state\n")
        columns = ("--format=%(refname) %(objectname) %(symref)",)
        refs = self.git("for-each-ref", *columns).stdout
        self.assertNotEqual(self.run_in_repo("git", "cat-file", "-e", commit).returncode, 0)

        tip = land_main.remote_branch_tip(self.repo, "origin", land_main.RemoteDefaultBranch.HEAD,
                                         time_limit=land_main.CommandTimeLimit(10))

        self.assertEqual((tip.branch, tip.commit), ("trunk", commit))
        self.assertEqual(self.git("cat-file", "-t", commit).stdout.strip(), "commit")
        self.assertEqual(self.git("for-each-ref", *columns).stdout, refs)
        self.assertEqual(fetch_head.read_text(), "saved fetch state\n")

    def test_uses_the_advertised_local_commit_when_fetch_would_fail(self) -> None:
        local = self.git("rev-parse", "HEAD").stdout.strip()
        self.fail_git("-C", str(self.repo), "-c", "remote.origin.followRemoteHEAD=never", "fetch")

        self.assertEqual(land_main.remote_branch_commit(self.repo, "origin", "main"), local)

    def test_push_runs_the_hook_from_the_checkout_root_with_the_full_commit(self) -> None:
        self.install_hook()
        expected: list[str] = []
        for branch in ("main", "trunk"):
            with self.subTest(branch=branch):
                if branch != "main":
                    _ = self.git("push", "origin", "HEAD:refs/heads/trunk")
                    _ = self.git("--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/trunk")
                commit = self.next_commit(branch + "\n")
                nested = self.write(self.repo / "nested/fixture", "fixture\n").parent
                result = land_main.land(nested, commit[:10], branch)
                self.assertIsInstance(result.push, land_main.Pushed)
                self.assertIsInstance(result.hook, land_main.HookDone)
                self.assertEqual((result.commit, result.branch), (commit, branch))
                self.assertEqual(self.git("ls-remote", "origin", f"refs/heads/{branch}").stdout.split()[0], commit)
                expected.append(f"{self.repo}|{commit}|{branch}")
                self.assertEqual(self.hook_runs(), expected)
                self.assertEqual(self.completion().read_text(), commit + "\n")

    def test_an_unchanged_tip_with_a_completion_record_skips_the_push_and_hook(self) -> None:
        self.install_hook()
        commit = self.next_commit()
        _ = land_main.land(self.repo, commit)
        saved = self.completion().read_bytes(), self.completion().stat().st_mtime_ns
        with mock.patch.object(subprocess, "run", wraps=subprocess.run) as commands:
            repeated = land_main.land(self.repo, commit)
        self.assertIsInstance(repeated.push, land_main.AlreadyLanded)
        self.assertIsInstance(repeated.hook, land_main.HookSkipped)
        self.assertEqual(self.hook_runs(), [f"{self.repo}|{commit}|main"])
        self.assertEqual((self.completion().read_bytes(), self.completion().stat().st_mtime_ns), saved)
        self.assertFalse(any("push" in cast(list[str], call.args[0]) for call in commands.call_args_list))

    def test_landing_git_commands_use_only_the_requested_config(self) -> None:
        inherited = "'core.fsync'='none'"
        settings = ("Core.Fsync=committed",)
        appended = "'Core.Fsync'='committed'"
        for use_config in (True, False):
            with self.subTest(use_config=use_config), mock.patch.dict(os.environ, {"GIT_CONFIG_PARAMETERS": inherited}):
                commit = self.next_commit(f"Config {use_config}\n")
                expected_environment = ({**os.environ, "GIT_CONFIG_PARAMETERS": f"{inherited} {appended}"}
                                        if use_config else None)
                with mock.patch.object(subprocess, "run", wraps=subprocess.run) as commands:
                    if use_config:
                        result = land_main.land(self.repo, commit, git_config=settings)
                    else:
                        result = land_main.land(self.repo, commit)
                self.assertIsInstance(result.push, land_main.Pushed)
                prefix = ["git", "-C", str(self.repo)]
                for command in ([*prefix, "ls-remote", "--refs", "--", "origin", "refs/heads/main"],
                                [*prefix, "push", "origin", f"{commit}:refs/heads/main"]):
                    commands.assert_any_call(command, capture_output=True, text=True, check=False,
                                             timeout=None, env=expected_environment)
                self.assertEqual(os.environ["GIT_CONFIG_PARAMETERS"], inherited)

    def test_landing_refuses_hook_settings_before_running_git(self) -> None:
        for key in ("core.hooksPath", "hook.secret-check.event"):
            with self.subTest(key=key), mock.patch.object(subprocess, "run") as commands:
                with self.assertRaises(land_main.LandError) as error:
                    _ = land_main.land(self.repo, "HEAD", git_config=(f"{key}=invalid",))
                self.assertIn(key, str(error.exception))
                self.assertIn("core.fsync", str(error.exception))
                commands.assert_not_called()

    def test_a_failed_hook_keeps_the_cli_successful_and_retries_on_the_same_tip(self) -> None:
        self.install_hook()
        _ = self.write(Path(self.environment["TEST_HOOK_FAIL"]), "fail\n")
        commit = self.next_commit()
        first = self.run_in_repo(sys.executable, str(SCRIPTS / "lib/land_main.py"),
                                 "--checkout", str(self.repo), "--commit", commit)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn(f"land_main: pushed {commit} on main", first.stdout)
        self.assertIn("post-push hook: failed (exit 9); rerun:", first.stderr)
        self.assertIn(f"LANDED_SHA={commit}", first.stderr)
        self.assertIn("LANDED_BRANCH=main", first.stderr)
        self.assertFalse(self.completion().exists())
        self.assertEqual(self.git("rev-parse", "origin/main").stdout.strip(), commit)
        failed = land_main.land(self.repo, commit)
        self.assertIsInstance(failed.push, land_main.AlreadyLanded)
        self.assertIsInstance(failed.hook, land_main.HookFailed)
        assert isinstance(failed.hook, land_main.HookFailed)
        self.assertEqual(failed.hook.exit_code, 9)
        self.assertIn(failed.hook.rerun_command, first.stderr)
        Path(self.environment["TEST_HOOK_FAIL"]).unlink()
        repeated = land_main.land(self.repo, commit)
        self.assertIsInstance(repeated.push, land_main.AlreadyLanded)
        self.assertIsInstance(repeated.hook, land_main.HookDone)
        self.assertEqual(self.hook_runs(), [f"{self.repo}|{commit}|main"] * 3)
        self.assertEqual(self.completion().read_text(), commit + "\n")

    def test_a_failed_hook_rerun_keeps_the_requested_git_config(self) -> None:
        self.install_hook()
        _ = self.write(Path(self.environment["TEST_HOOK_FAIL"]), "fail\n")
        commit = self.next_commit()
        settings = ("core.fsync=committed",)
        failed = land_main.land(self.repo, commit, git_config=settings)
        assert isinstance(failed.hook, land_main.HookFailed)
        self.assertIn(" --git-config core.fsync=committed", failed.hook.rerun_command)

        Path(self.environment["TEST_HOOK_FAIL"]).unlink()
        # The lock path read in main_push_lock is outside the landing Git calls.
        with mock.patch.object(land_main, "run_git", wraps=land_main.run_git) as commands:
            self.assertIsInstance(land_main.retry_hook(self.repo, commit, "main", "", git_config=settings),
                                  land_main.HookDone)
        self.assertTrue(commands.call_args_list)
        for call in commands.call_args_list:
            environment = cast(dict[str, str] | None, call.kwargs.get("environment"))
            assert environment is not None, call
            self.assertEqual(environment["GIT_CONFIG_PARAMETERS"], "'core.fsync'='committed'")

    def test_a_commit_behind_origin_skips_the_push_and_hook(self) -> None:
        self.install_hook()
        commit = self.next_commit("older\n")
        newer = self.next_commit("newer\n")
        _ = self.git("push", "origin", "main")
        with mock.patch.object(subprocess, "run", wraps=subprocess.run) as commands:
            result = land_main.land(self.repo, commit)
        self.assertIsInstance(result.push, land_main.AlreadyLanded)
        self.assertIsInstance(result.hook, land_main.HookSkipped)
        self.assertEqual(result.commit, commit)
        self.assertEqual(self.git("rev-parse", "origin/main").stdout.strip(), newer)
        self.assertFalse(any("push" in cast(list[str], call.args[0]) for call in commands.call_args_list))
        self.assertEqual(self.hook_runs(), [])
        self.assertFalse(self.completion().exists())

    def test_a_diverged_origin_refuses_the_landing_without_running_the_hook(self) -> None:
        self.install_hook()
        _ = self.git("switch", "-c", "other")
        remote = self.next_commit("remote\n")
        _ = self.git("push", "origin", "HEAD:refs/heads/main")
        _ = self.git("switch", "main")
        local = self.next_commit("local\n")
        with self.assertRaises(land_main.NotFastForward):
            _ = land_main.land(self.repo, local)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/main").stdout.split()[0], remote)
        self.assertEqual(self.hook_runs(), [])
        self.assertFalse(self.completion().exists())

    def test_an_unreadable_completion_record_runs_the_hook_after_landing(self) -> None:
        self.install_hook()
        commit = self.next_commit()
        record = self.completion()
        _ = self.write(record, commit + "\n")
        read_text = Path.read_text

        def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == record:
                raise PermissionError("unreadable completion record")
            return read_text(path, encoding=encoding, errors=errors)

        with mock.patch.object(Path, "read_text", new=read):
            result = land_main.land(self.repo, commit)
        self.assertIsInstance(result.push, land_main.Pushed)
        self.assertIsInstance(result.hook, land_main.HookDone)
        self.assertEqual(self.git("rev-parse", "origin/main").stdout.strip(), commit)
        self.assertEqual(self.hook_runs(), [f"{self.repo}|{commit}|main"])
        self.assertEqual(record.read_text(), commit + "\n")

    def test_a_feature_branch_skips_the_behavior_check_even_with_an_altered_test(self) -> None:
        self.install_hook()
        test = self.write(self.repo / "test_feature.py", "def test_fixture():\n    assert 1\n")
        _ = self.git("add", test.name)
        _ = self.next_commit()
        _ = self.git("push", "origin", "HEAD:refs/heads/feature")
        _ = self.write(test, "def test_fixture():\n    assert 2\n")
        main = self.git("rev-parse", "origin/main").stdout.strip()
        commit = self.next_commit()
        with mock.patch.object(behavior_check, "require_review", wraps=behavior_check.require_review) as check:
            result = land_main.land(self.repo, commit, "feature")
        check.assert_not_called()
        self.assertIsInstance(result.push, land_main.Pushed)
        self.assertIsInstance(result.hook, land_main.HookSkipped)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/feature").stdout.split()[0], commit)
        self.assertEqual(self.git("rev-parse", "origin/main").stdout.strip(), main)
        self.assertEqual(self.hook_runs(), [])
        self.assertFalse(self.completion().exists())

    def test_the_remote_default_branch_requires_review_even_with_a_stale_local_head(self) -> None:
        test = self.write(self.repo / "test_default.py", "def test_fixture():\n    assert 1\n")
        _ = self.git("add", test.name)
        base = self.next_commit()
        _ = self.git("push", "origin", "HEAD:refs/heads/trunk", "HEAD:refs/heads/main")
        _ = self.git("--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/trunk")
        _ = self.write(test, "def test_fixture():\n    assert 2\n")
        head = self.next_commit()
        nested = self.repo / "nested"
        nested.mkdir()
        for local_head in ("missing", "stale"):
            with self.subTest(local_head=local_head):
                if local_head == "stale":
                    _ = self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
                with self.assertRaisesRegex(land_main.LandError, "no review is on record"):
                    _ = land_main.land(nested, head, "trunk")
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/trunk").stdout.split()[0], base)
        with mock.patch.object(behavior_check, "require_review", wraps=behavior_check.require_review) as check:
            result = land_main.land(self.repo, head, "main")
        check.assert_not_called()
        self.assertIsInstance(result.push, land_main.Pushed)

    def test_a_checkout_without_a_hook_lands_and_reports_the_absent_hook(self) -> None:
        commit = self.next_commit()
        result = land_main.land(self.repo, commit)
        self.assertIsInstance(result.push, land_main.Pushed)
        self.assertIsInstance(result.hook, land_main.HookAbsent)
        self.assertEqual(self.git("rev-parse", "origin/main").stdout.strip(), commit)
        self.assertFalse(self.completion().exists())

    def test_the_landing_cli_reuses_the_parent_cli_lock_hold(self) -> None:
        self.install_hook()
        commit = self.next_commit()
        result = self.run_in_repo(
            str(SCRIPTS / "lib/py"), str(SCRIPTS / "lib/main_push_lock.py"),
            "--checkout", str(self.repo), "--", str(SCRIPTS / "lib/py"),
            str(SCRIPTS / "lib/land_main.py"), "--checkout", str(self.repo), "--commit", commit,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"land_main: pushed {commit} on main", result.stdout)
        self.assertIn("post-push hook: done", result.stdout)
        self.assertEqual(self.git("rev-parse", "origin/main").stdout.strip(), commit)
        self.assertEqual(self.hook_runs(), [f"{self.repo}|{commit}|main"])
        self.assertEqual(len((self.repo / ".git" / main_push_lock.RECORD_NAME).read_text().splitlines()), 1)

    def test_the_land_requires_clear_or_yes_for_an_altered_test(self) -> None:
        test = self.write(self.repo / "test_approval.py", "def test_fixture():\n    assert 1\n")
        _ = self.git("add", test.name)
        base = self.next_commit("test base\n")
        _ = self.git("push", "origin", "main")
        _ = self.write(test, "def test_fixture():\n    assert 2\n")
        head = self.next_commit("alter a test\n")
        change = behavior_check.GitRange(self.repo, base, head)
        tests = behavior_check.changed_tests(change)
        assert isinstance(tests, behavior_check.ChangedTests)
        key = tests.key
        no = behavior_check.Answered("Q1", "fixture-id", behavior_check.Approval.NO, "No", datetime.now(timezone.utc))
        waiting = behavior_check.Waiting("Q1", "fixture-id")
        for record in (behavior_check.NoReview.MISSING, waiting, no):
            with self.subTest(record=record):
                if record is not behavior_check.NoReview.MISSING:
                    behavior_check.write_record(key, record)
                with self.assertRaises(land_main.LandError) as refused:
                    _ = land_main.land(self.repo, head)
                self.assertIn("test_approval.py::test_fixture", str(refused.exception))
                self.assertIn(change.command("run"), str(refused.exception))
                self.assertEqual(self.git("ls-remote", "origin", "main").stdout.split()[0], base)
        behavior_check.write_record(key, behavior_check.Clear(self.root / "reply.txt"))
        with self.assertRaisesRegex(land_main.LandError, "no valid proof"):
            _ = land_main.land(self.repo, head)

        folder = self.root / "review"
        reply = self.write(folder / "completed-reply.txt", '[{"test":"test_approval.py::test_fixture","finding":"none"}]')
        _ = self.write(folder / "review-key", key)
        _ = self.write(behavior_check.record_path(key).with_suffix(".run"), str(folder))
        behavior_check.write_record(key, behavior_check.Clear(reply))
        with mock.patch.object(unit_runs, "ASTRA_RUN_ROOT", self.root):
            self.assertIsInstance(land_main.land(self.repo, head).push, land_main.Pushed)

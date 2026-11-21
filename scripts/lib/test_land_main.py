"""Land commits and retry hooks against a private local origin."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import cast, override
from unittest import mock

from scripts.lib import land_main, main_push_lock
from scripts.lib.rerun_test_support import SCRIPTS, ScriptRepositoryTestCase


class LandMainTests(ScriptRepositoryTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.environment.update(TEST_HOOK_LOG=str(self.root / "hook-runs"),
                                TEST_HOOK_FAIL=str(self.root / "hook-fails"))
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

    def test_push_runs_the_hook_from_the_checkout_root_with_the_full_commit(self) -> None:
        self.install_hook()
        expected: list[str] = []
        for branch in ("main", "trunk"):
            with self.subTest(branch=branch):
                if branch != "main":
                    _ = self.git("push", "origin", "HEAD:refs/heads/trunk")
                    _ = self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
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

    def test_a_feature_branch_lands_without_running_the_default_branch_hook(self) -> None:
        self.install_hook()
        _ = self.git("push", "origin", "HEAD:refs/heads/feature")
        main = self.git("rev-parse", "origin/main").stdout.strip()
        commit = self.next_commit()
        result = land_main.land(self.repo, commit, "feature")
        self.assertIsInstance(result.push, land_main.Pushed)
        self.assertIsInstance(result.hook, land_main.HookSkipped)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/feature").stdout.split()[0], commit)
        self.assertEqual(self.git("rev-parse", "origin/main").stdout.strip(), main)
        self.assertEqual(self.hook_runs(), [])
        self.assertFalse(self.completion().exists())

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

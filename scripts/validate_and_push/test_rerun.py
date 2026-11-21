"""Validation and merged-branch recovery use only a local bare remote."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import cast
import unittest
from unittest.mock import patch

from scripts.lib.rerun_test_support import ScriptRepositoryTestCase


class ValidationFixtureTests(unittest.TestCase):
    def test_the_repository_fixture_uses_a_private_cache(self) -> None:
        caller_cache = self.enterContext(tempfile.TemporaryDirectory())
        fixture = ScriptRepositoryTestCase()
        self.addCleanup(fixture.doCleanups)
        with patch.dict(os.environ, XDG_CACHE_HOME=caller_cache):
            fixture.setUp()

        self.assertEqual(fixture.environment["XDG_CACHE_HOME"], str(fixture.home / ".cache"))


class ValidationRerunTests(ScriptRepositoryTestCase):
    def test_dispatch_and_poll_report_the_run_or_error(self) -> None:
        self.seed()
        _ = self.git("switch", "-c", "ci-delay")
        _ = self.git("push", "origin", "ci-delay")
        published = self.git("rev-parse", "HEAD").stdout.strip()
        repo = "github.com/example/example"
        self.environment.update(CI_PUSH_ORIGIN=str(self.remote), CI_PUSH_REPO=repo, CANCEL_PRIOR="false")
        calls = self.root / "gh-calls"
        lookup_error = self.root / "lookup-error"
        listings = self.root / "poll-runs.json"
        _ = self.stub("gh", f'''import json, sys
from pathlib import Path
args = sys.argv[1:]
with Path({str(calls)!r}).open("a") as log:
    log.write(json.dumps(args[:2]) + "\\n")
if args[:2] == ["workflow", "run"]:
    print("Dispatch was refused", file=sys.stderr)
    raise SystemExit(9)
if args[:2] != ["run", "list"]:
    raise SystemExit("Unexpected gh command")
if Path({str(lookup_error)!r}).exists():
    print("Could not list runs", file=sys.stderr)
    raise SystemExit(8)
print(Path({str(listings)!r}).read_text())
''')
        _ = self.stub("sleep", 'raise SystemExit("No eligible run in the first poll")\n')
        created_at = "2026-10-30T00:01:00Z"
        at_bound = (123, "queued", "")
        queued = (124, "queued", "")
        failed = (125, "completed", "failure")
        dispatch_args = ("--dispatch-published", published, "ci.yml")
        follow_args = ("--follow-published", published, "pushed", "ci.yml", "workflow_dispatch")
        poll_args = (*follow_args, f"dispatched 123 {created_at}")
        list_call = '["run", "list"]'

        for outcome, args, runs, exit_code, message, gh_calls in (
            ("lookup error", dispatch_args, (), 8, "Could not list runs", [list_call]),
            ("poll error", (*follow_args, "dispatched 0"), (), 8, "Could not list runs", [list_call]),
            ("refused", dispatch_args, (), 9, "Dispatch was refused", [list_call, '["workflow", "run"]']),
            ("queued and failed", poll_args, (at_bound, queued, failed), 0, "run_id:  124", [list_call]),
            ("failed before first poll", poll_args, (at_bound, failed), 1,
             f"CI run 125 completed with failure (https://{repo}/actions/runs/125)", [list_call]),
            ("moved", dispatch_args, (), 1, f"origin/ci-delay moved before dispatch of {published}", [list_call]),
        ):
            with self.subTest(outcome=outcome):
                if outcome in ("lookup error", "poll error"):
                    lookup_error.touch()
                else:
                    lookup_error.unlink(missing_ok=True)
                if outcome == "moved":
                    _ = self.git("commit", "--allow-empty", "-m", "Advance the published branch")
                    _ = self.git("push", "origin", "ci-delay")
                _ = calls.write_text("")
                _ = self.write(listings, json.dumps([
                    {"databaseId": run_id, "status": status, "conclusion": conclusion,
                     "createdAt": created_at, "headSha": published}
                    for run_id, status, conclusion in runs
                ]))

                result = self.script("validate_and_push/push_direct.sh", *args)

                self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                self.assertIn(message, result.stderr if exit_code else result.stdout)
                self.assertEqual(calls.read_text().splitlines(), gh_calls)

    def install_gh(self) -> None:
        _ = self.stub("gh", '''import subprocess, sys
args = sys.argv[1:]
if args[:2] == ["repo", "view"]:
    print("main")
elif args[:2] == ["pr", "view"]:
    if "mergeCommit" in args:
        subprocess.run(["git", "rev-parse", "main"], check=True)
    else:
        print(7)
elif args[:2] == ["pr", "checks"]:
    print("checks pass")
elif args[:2] == ["pr", "merge"]:
    subprocess.run(["git", "switch", "main"], check=True)
    subprocess.run(["git", "merge", "--ff-only", "retry-pr"], check=True)
    subprocess.run(["git", "push", "origin", "main"], check=True)
else:
    sys.exit("unexpected gh call")
''')

    def install_autofix(self) -> None:
        lint = self.stub("lint", '''import os, sys
from pathlib import Path
if sys.argv[1] == "fmt":
    Path("seed.txt").write_text("formatted\\n")
    stop = Path(os.environ["HOME"]) / "stop-formatter"
    if stop.exists():
        stop.unlink()
        sys.exit(86)
''')
        target = self.home / ".claude/scripts/lint/lint"
        target.parent.mkdir(parents=True)
        _ = shutil.copy2(lint, target)
        _ = self.stub("taplo", "pass\n")
        self.environment["VALIDATE_QUICK"] = "1"

    @unittest.skipUnless(sys.platform == "linux", "requires a Linux process start")
    def test_full_validation_shares_one_run_place_with_local_ci(self) -> None:
        calls = self.root / "ci-steps"
        record = f'''import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
from scripts.lib.process_start import Started, proc_table_start

owner = os.getppid()
start = proc_table_start(owner, Path("/proc"))
if not isinstance(start, Started):
    raise SystemExit("Cannot read the parent process start")
parent_run = f"{{owner}}:{{start.start}}"
with Path({str(calls)!r}).open("a") as log:
    log.write(json.dumps([Path(sys.argv[0]).name, sys.argv[1:], parent_run,
                          os.environ.get("BUILDLOG_RUN", "")]) + "\\n")
'''
        lint = self.stub("lint", record)
        installed = self.home / ".claude/scripts/lint/lint"
        installed.parent.mkdir(parents=True)
        _ = shutil.copy2(lint, installed)
        _ = self.stub("taplo", record)
        _ = self.stub("record-ci-step", record)
        target = "x86_64-pc-windows-gnu"
        _ = self.stub("rustup", f"print({target!r})\n")
        _ = self.write(self.repo / ".claude/config/cross_targets", target + "\n")
        _ = self.write(self.repo / ".claude/config/local_ci.sh", "record-ci-step first\nrecord-ci-step second\n")
        self.seed()
        _ = self.environment.pop("BUILDLOG_RUN", None)
        _ = self.environment.pop("VERIFY_PROC_ROOT", None)

        result = self.script("validate_and_push/validate_ci.sh")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [cast(tuple[str, list[str], str, str], json.loads(line)) for line in calls.read_text().splitlines()]
        self.assertEqual([(tool, args) for tool, args, _, _ in rows], [
            ("lint", ["mend", "--workspace", "--fix"]),
            ("lint", ["fmt"]),
            ("taplo", ["fmt"]),
            ("lint", ["clippy", "--workspace", "--all-features"]),
            ("lint", ["clippy", "--workspace", "--all-features", "--target", target]),
            ("lint", ["doc", "--workspace", "--all-features"]),
            ("lint", ["nextest", "--workspace", "--all-features", "--tests"]),
            ("lint", ["mend", "--workspace", "--fail-on-warn"]),
            ("record-ci-step", ["first"]),
            ("record-ci-step", ["second"]),
        ])
        _, _, validation_run, _ = rows[0]
        self.assertEqual({step_run for _, _, _, step_run in rows}, {validation_run})

    def test_a_synced_default_branch_retries_its_failed_post_push_hook(self) -> None:
        hook = '''#!/usr/bin/env bash
set -euo pipefail
echo "$LANDED_SHA" >> .git/hook-runs
if [[ ! -f hook-started ]]; then touch hook-started; exit 9; fi
touch hook-finished
'''
        _ = self.write(self.repo / ".claude/config/post_push.sh", hook)
        self.seed()
        _ = self.write(self.repo / "seed.txt", "next\n")
        _ = self.git("commit", "-am", "next")
        self.install_gh()
        stopped = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("post-push hook: failed (exit 9); rerun:", stopped.stderr)
        self.assertIn("LANDED_SHA=", stopped.stderr)
        self.assertIn("LANDED_BRANCH=main", stopped.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, self.git("rev-parse", "origin/main").stdout)
        landed = self.git("rev-parse", "HEAD").stdout
        finished = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertTrue((self.repo / "hook-finished").exists())
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, landed)
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "2")
        repeated = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual((self.repo / ".git/hook-runs").read_text().splitlines(), [landed.strip()] * 2)

    def test_a_successful_post_push_hook_runs_once_per_landed_commit(self) -> None:
        _ = self.write(self.repo / ".claude/config/post_push.sh", 'echo "$LANDED_SHA" >> .git/hook-runs\n')
        self.seed()
        self.install_gh()
        first = self.git("rev-parse", "HEAD").stdout.strip()
        stop = self.write(self.root / "stop-completion", "stop\n")
        startup = self.root / "startup"
        _ = self.write(startup / "sitecustomize.py", f'''from pathlib import Path
from scripts.lib import atomic_file
write = atomic_file.write_text_atomic
def stop_after_completion(path, text, **options):
    write(path, text, **options)
    flag = Path({str(stop)!r})
    if path.name == "post-push-completed" and flag.exists():
        flag.unlink()
        raise SystemExit(86)
atomic_file.write_text_atomic = stop_after_completion
''')
        self.environment["PYTHONPATH"] = os.pathsep.join((str(startup), str(Path(__file__).resolve().parents[2])))
        stopped = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertNotEqual(stopped.returncode, 0)
        self.assertFalse(stop.exists(), stopped.stdout + stopped.stderr)
        self.assertEqual((self.repo / ".git/post-push-completed").read_text().strip(), first)
        repeated = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual((self.repo / ".git/hook-runs").read_text().splitlines(), [first])
        _ = self.write(self.repo / "seed.txt", "next\n")
        _ = self.git("commit", "-am", "next")
        _ = self.git("push", "origin", "main")
        second = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.repo / ".git/hook-runs").read_text().splitlines(), [first, second])

    def test_a_pending_hook_fast_forwards_before_reading_its_landed_commit(self) -> None:
        _ = self.write(self.repo / ".claude/config/post_push.sh", 'echo "$LANDED_SHA" >> .git/hook-runs\n')
        self.seed()
        self.install_gh()
        before = self.git("rev-parse", "HEAD").stdout.strip()
        _ = self.write(self.repo / "seed.txt", "remote tip\n")
        _ = self.git("commit", "-am", "remote tip")
        _ = self.git("push", "origin", "main")
        remote = self.git("rev-parse", "HEAD").stdout.strip()
        _ = self.git("reset", "--hard", before)
        result = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), remote)
        self.assertEqual((self.repo / ".git/hook-runs").read_text().splitlines(), [remote])

    def test_validation_finishes_its_failed_autofix_commit_on_rerun(self) -> None:
        self.seed()
        self.install_autofix()
        self.fail_git("commit")
        stopped = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(stopped.returncode, 0)
        self.assertTrue((self.repo / ".git/validate-autofix").exists())
        self.assertNotEqual(self.git("status", "--porcelain").stdout, "")
        finished = self.script("validate_and_push/validate_ci.sh")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual((self.repo / "seed.txt").read_text(), "formatted\n")
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertFalse((self.repo / ".git/validate-autofix").exists())
        self.assertEqual(self.git("show", "--format=", "--name-only", "HEAD").stdout.strip(), "seed.txt")

    def test_a_marker_saved_before_formatting_does_not_claim_later_user_edits(self) -> None:
        self.seed()
        self.install_autofix()
        before = self.git("rev-parse", "HEAD").stdout
        self.stop_command("mv", target=".git/validate-autofix", after=True)
        stopped = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(stopped.returncode, 0)
        _ = self.write(self.repo / "user.txt", "user bytes\n")
        self.assert_user_edit_refused(before)
        self.assertEqual((self.repo / "seed.txt").read_text(), "seed\n")

    def test_a_user_edit_after_the_saved_autofix_output_is_refused(self) -> None:
        self.seed()
        self.install_autofix()
        before = self.git("rev-parse", "HEAD").stdout
        self.fail_git("commit")
        stopped = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(stopped.returncode, 0)
        _ = self.write(self.repo / "user.txt", "user bytes\n")
        self.assert_user_edit_refused(before)
        self.assertEqual((self.repo / "seed.txt").read_text(), "formatted\n")

    def test_a_force_added_ignored_file_is_outside_the_saved_autofix_output(self) -> None:
        _ = self.write(self.repo / ".gitignore", "user.txt\n")
        self.seed()
        self.install_autofix()
        before = self.git("rev-parse", "HEAD").stdout
        self.fail_git("commit")
        stopped = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(stopped.returncode, 0)
        _ = self.write(self.repo / "user.txt", "user bytes\n")
        _ = self.git("add", "-f", "user.txt")
        self.assert_user_edit_refused(before)
        self.assertIn("user.txt", self.git("diff", "--cached", "--name-only").stdout)

    def test_an_interrupted_formatter_preserves_its_unrecorded_output_and_explains_recovery(self) -> None:
        self.seed()
        self.install_autofix()
        before = self.git("rev-parse", "HEAD").stdout
        index = (self.repo / ".git/index").read_bytes()
        _ = self.write(self.home / "stop-formatter", "stop\n")
        stopped = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(stopped.returncode, 0)
        result = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("formatter stopped before its output was recorded", result.stderr)
        self.assertIn("seed.txt", result.stderr)
        self.assertIn("Inspect git diff", result.stderr)
        self.assertIn("git checkout -- <paths> and rerun", result.stderr)
        self.assertIn("commit the changes yourself", result.stderr)
        self.assertEqual((self.repo / "seed.txt").read_text(), "formatted\n")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, before)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("diff", "--cached", "--name-only").stdout, "")

    def test_autofix_content_proofs_do_not_run_index_hooks(self) -> None:
        self.seed()
        self.install_autofix()
        hook_log = self.root / "index-hook-runs"
        self.environment["TEST_INDEX_HOOK_LOG"] = str(hook_log)
        hook = self.write(self.repo / ".git/hooks/post-index-change", '''#!/bin/sh
printf '%s\\n' "${GIT_INDEX_FILE:-real}" >> "$TEST_INDEX_HOOK_LOG"
''')
        hook.chmod(0o755)
        result = self.script("validate_and_push/validate_ci.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = hook_log.read_text().splitlines()
        self.assertTrue(calls, "The real commit still runs repository hooks")
        self.assertFalse(any("validate-index." in call for call in calls), calls)
        self.assertEqual((self.repo / "seed.txt").read_text(), "formatted\n")

    def test_autofix_proof_refuses_a_same_size_edit_at_the_index_mtime(self) -> None:
        self.seed()
        self.install_autofix()
        _ = self.git("config", "core.trustctime", "false")
        source = self.write(self.repo / "racy.txt", "before\n")
        timestamp = 1_600_000_000_000_000_000
        os.utime(source, ns=(timestamp, timestamp))
        _ = self.git("add", "racy.txt")
        _ = self.git("commit", "-qm", "Add the racy file")
        head = self.git("rev-parse", "HEAD").stdout.strip()
        tree = self.git("rev-parse", "HEAD^{tree}").stdout.strip()
        _ = self.write(self.repo / ".git/validate-autofix", f"{head} {tree}\n")
        index = self.repo / ".git/index"
        os.utime(index, ns=(timestamp, timestamp))
        _ = source.write_text("edited\n")
        os.utime(source, ns=(timestamp, timestamp))

        result = self.script("validate_and_push/validate_ci.sh")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uncommitted changes outside the saved autofix output", result.stderr)
        self.assertIn("racy.txt", result.stderr)
        self.assertEqual(source.read_text(), "edited\n")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), head)

    def assert_user_edit_refused(self, before: str) -> None:
        index = (self.repo / ".git/index").read_bytes()
        result = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("user.txt", result.stderr)
        self.assertEqual((self.repo / "user.txt").read_text(), "user bytes\n")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, before)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_validation_still_refuses_unowned_dirty_work(self) -> None:
        self.seed()
        _ = self.write(self.repo / "seed.txt", "user edit\n")
        result = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uncommitted changes", result.stdout)

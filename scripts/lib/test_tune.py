"""An approved value change is validated, committed alone, pushed and pulled or queued."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import fcntl
import shlex
import socket
import subprocess
import sys
import time
from typing import override
import unittest
from unittest import mock

from scripts.lib import live_settings, main_publish, models_registry, tune
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.test_main_publish import PublicationTestCase

SCRIPT = Path(__file__).with_name("tune.py")


class TuneTests(PublicationTestCase):
    @override
    def setUp(self) -> None:
        (self.repository.home / ".claude").symlink_to(self.repository.checkout, target_is_directory=True)
        for name in ("basedpyright", "pytest", "cargo"):
            program = self.repository.programs / name
            _ = program.write_text('#!/bin/sh\nprintf test >> "$HOME/unexpected-check"\nexit 99\n')
            program.chmod(0o755)

    def run_tune(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments], cwd=self.repository.checkout,
            env=self.repository.env, capture_output=True, text=True, check=False,
            timeout=TEST_HANG_GUARD_SECS,
        )

    def snapshot(self) -> tuple[str, str, str, bytes, int, bytes]:
        repo = self.repository
        return (
            repo.head(), repo.remote_tip(), repo.git(repo.checkout, "status", "--porcelain=v1"),
            repo.config.read_bytes(), repo.config.stat().st_mtime_ns,
            (repo.checkout / ".git/index").read_bytes(),
        )

    def test_invalid_file_key_value_or_empty_approval_refuses_without_changing_anything(self) -> None:
        cases = (
            ("missing", "amount", "3", "valid reason", "approved"),
            ("example", "missing", "3", "valid reason", "approved"),
            ("example", "amount", "0", "valid reason", "approved"),
            ("example", "amount", "11", "valid reason", "approved"),
            ("example", "amount", "bad", "valid reason", "approved"),
            ("example", "mode", "unknown", "valid reason", "approved"),
            ("example", "amount", "3", "", "approved"),
            ("example", "amount", "3", "   ", "approved"),
            ("example", "amount", "3", "valid reason", ""),
            ("example", "amount", "3", "valid reason", "   "),
        )
        for file, key, value, why, approval in cases:
            with self.subTest(file=file, key=key, value=value, why=why, approval=approval):
                before = self.snapshot()
                result = self.run_tune(file, key, value, "--why", why, "--user-words", approval)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.repository.ssh_log.exists())
                self.assertFalse((self.repository.root / "state").exists())

    def test_approved_change_commits_only_its_file_with_reason_and_user_trailer_then_pulls_mac(self) -> None:
        repo = self.repository
        before = repo.config.read_text()
        _ = (repo.checkout / "other.txt").write_text("unrelated staged edit\n")
        _ = repo.git(repo.checkout, "add", "other.txt")
        result = self.run_tune("example", "amount", "3", "--why", "Keep builds responsive",
                               "--user-words", "yes set it to 3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(repo.config.read_text(), before.replace("amount=2", "amount=3"))
        self.assertEqual(repo.git(repo.checkout, "log", "-1", "--format=%s"), "tune(example): amount 2 -> 3")
        message = repo.git(repo.checkout, "log", "-1", "--format=%B")
        self.assertIn("\nKeep builds responsive\n", message)
        self.assertIn("User-Approved: yes set it to 3", message)
        self.assertEqual(repo.git(repo.checkout, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"),
                         "config/example.conf")
        self.assertEqual(repo.git(repo.checkout, "diff", "--cached", "--name-only"), "other.txt")
        self.assertEqual(repo.remote_tip(), repo.head())
        self.assertIn("git fetch -q --no-write-fetch-head", repo.ssh_log.read_text())
        self.assertIn(str(repo.checkout), repo.ssh_log.read_text())
        self.assertFalse((repo.root / "state/mac.json").exists())
        self.assertFalse((repo.home / "unexpected-check").exists())

    def test_approved_change_publishes_a_pending_installed_zone_commit_before_pulling_the_mac(self) -> None:
        repo = self.repository
        zone_file = repo.checkout / "config/user_zone.conf"
        _ = zone_file.write_text("zone=America/Curacao\n")
        _ = repo.git(repo.checkout, "add", "config/user_zone.conf")
        _ = repo.git(repo.checkout, "commit", "-qm", "user zone: America/Curacao (AST)")
        zone_commit = repo.head()
        self.assertNotEqual(zone_commit, repo.remote_tip())

        result = self.run_tune("example", "amount", "3", "--why", "Approved tuning",
                               "--user-words", "yes")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(repo.remote_tip(), repo.head())
        self.assertEqual(repo.git(repo.origin, "rev-parse", "main^"), zone_commit)
        self.assertEqual(repo.git(repo.origin, "show", "main:config/user_zone.conf"),
                         "zone=America/Curacao")
        self.assertIn("amount=3", repo.git(repo.origin, "show", "main:config/example.conf"))
        self.assertIn("git fetch -q --no-write-fetch-head", repo.ssh_log.read_text())
        self.assertFalse((repo.root / "state/mac.json").exists())

    def test_already_set_value_prints_one_line_without_a_write_and_completes_the_mac_pull(self) -> None:
        before = self.snapshot()
        result = self.run_tune("example", "amount", "2", "--why", "Already approved", "--user-words", "yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "already amount=2\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.snapshot(), before)
        self.assertIn("git fetch -q --no-write-fetch-head", self.repository.ssh_log.read_text())

    def waiting(self) -> list[live_settings.WaitingChange]:
        with mock.patch.dict(os.environ, self.repository.env, clear=True), live_settings._journal() as journal:  # pyright: ignore[reportPrivateUsage]
            return list(journal)

    def test_a_change_is_live_here_and_on_the_mac_while_main_is_locked_and_main_gets_it_after(self) -> None:
        repo = self.repository
        lock = (repo.checkout / ".git/main-push.lock").open("a")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)
        running = subprocess.Popen(
            [sys.executable, str(SCRIPT), "example", "amount", "3", "--why", "Approved tuning", "--user-words", "yes"],
            cwd=repo.checkout, env=repo.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        while not (repo.ssh_log.exists() and "live_settings.py" in repo.ssh_log.read_text()) \
                and running.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(running.poll())
        self.assertIn("amount=3", repo.config.read_text())
        self.assertIn("amount=2", repo.git(repo.checkout, "show", "HEAD:config/example.conf"))
        self.assertIn(shlex.join(["example", "amount", "3"]), repo.ssh_log.read_text())
        fcntl.flock(lock, fcntl.LOCK_UN)
        output, errors = running.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(running.returncode, 0, output + errors)
        self.assertEqual(output.splitlines()[:2], ["live now: amount=3", "mac: live now: amount=3"])
        self.assertIn("amount=3", repo.git(repo.origin, "show", "main:config/example.conf"))
        self.assertEqual(repo.git(repo.checkout, "status", "--porcelain=v1"), "")
        self.assertEqual(self.waiting(), [])

    def test_a_refused_main_keeps_the_value_live_and_the_notes_pass_publishes_it(self) -> None:
        repo = self.repository
        _ = (repo.checkout / "other.txt").write_text("unrelated local commit\n")
        _ = repo.git(repo.checkout, "commit", "-qam", "unrelated work")
        result = self.run_tune("example", "amount", "3", "--why", "Approved tuning", "--user-words", "yes")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("amount=3 stays live; main does not have it", result.stderr)
        self.assertIn("amount=3", repo.config.read_text())
        self.assertEqual([change.value for change in self.waiting()], ["3"])
        _ = repo.git(repo.checkout, "reset", "-q", "--keep", "HEAD^")
        with mock.patch.dict(os.environ, repo.env, clear=True), \
                mock.patch.object(models_registry, "publish_pending", return_value=False):
            lines = live_settings.publish_waiting()
        self.assertEqual(lines, ["main: pushed", "mac: pulled"])
        self.assertEqual(repo.git(repo.origin, "log", "-1", "--format=%s", "main"), "tune(example): amount 2 -> 3")
        self.assertEqual(self.waiting(), [])
        with mock.patch.dict(os.environ, repo.env, clear=True), \
                mock.patch.object(models_registry, "publish_pending", return_value=False):
            self.assertEqual(live_settings.publish_waiting(), [])

    def test_a_superseded_waiting_value_gets_no_commit_and_the_newest_value_reaches_main(self) -> None:
        repo = self.repository
        with mock.patch.dict(os.environ, repo.env, clear=True):
            for value in ("3", "4"):
                _ = live_settings.stage(live_settings.WaitingChange(
                    "example", "amount", value, f"tune(example): amount -> {value}"))
            lines = live_settings.publish(repo.checkout, wait=True)
        self.assertEqual(lines, ["main: amount=3 has a newer live value; no commit", "main: pushed", "mac: pulled"])
        self.assertEqual(repo.git(repo.origin, "log", "--format=%s", "-1", "main"), "tune(example): amount -> 4")
        self.assertIn("amount=4", repo.git(repo.origin, "show", "main:config/example.conf"))
        self.assertEqual(repo.git(repo.checkout, "status", "--porcelain=v1"), "")

    def test_the_readers_of_a_changed_file_start_at_once_in_their_own_session(self) -> None:
        repo = self.repository
        _ = (repo.config.parent / "live_settings.conf").write_text("readers=example:quota other:notes\n")
        with mock.patch.dict(os.environ, repo.env, clear=True), mock.patch.object(subprocess, "Popen") as started:
            self.assertEqual(live_settings.start_readers("example"), ["started now: the quota pass"])
            self.assertEqual(live_settings.start_readers("missing"), [])
        self.assertEqual(started.call_count, 1)
        self.assertEqual(started.call_args.args[0][-2:], ["--now", "quota"])
        self.assertTrue(started.call_args.kwargs["start_new_session"])

    def test_a_change_starts_the_readers_of_its_file(self) -> None:
        with mock.patch.dict(os.environ, self.repository.env, clear=True), \
                mock.patch.object(live_settings, "start_readers", return_value=["started now: the quota pass"]) as readers, \
                redirect_stdout(io.StringIO()) as output:
            status = tune.main(["example", "amount", "3", "--why", "Approved tuning", "--user-words", "yes"])
        self.assertEqual(status, 0, output.getvalue())
        readers.assert_called_once_with("example")
        self.assertIn("started now: the quota pass", output.getvalue())

    def test_ambiguous_base_key_refuses_without_a_write_and_names_both_lines(self) -> None:
        repo = self.repository
        host = socket.gethostname().split(".", 1)[0]
        original = (
            "# whole: 1..10\namount=2\n# whole: 1..10\n"
            + f"amount.{host}=5 # host capacity\namount.{host}=8 # ignored duplicate\n"
            + "# one of: fast, slow\nmode=fast\n"
        )
        _ = repo.config.write_text(original)
        _ = repo.git(repo.checkout, "commit", "-qam", "host configuration")
        _ = repo.git(repo.checkout, "push", "-q", "origin", "main")
        before = self.snapshot()
        result = self.run_tune("example", "amount", "6", "--why", "Approved host capacity", "--user-words", "yes")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("amount at line 2", result.stderr)
        self.assertIn(f"amount.{host} at line 4", result.stderr)
        self.assertFalse(repo.ssh_log.exists())

    def test_exact_host_key_edits_its_first_line_and_preserves_comments(self) -> None:
        repo = self.repository
        host_key = "amount.mac"
        original = (
            "# whole: 1..10\namount=2\n# whole: 1..10\n"
            + f"{host_key}=5 # host capacity\n{host_key}=8 # ignored duplicate\n"
            + "# one of: fast, slow\nmode=fast\n"
        )
        _ = repo.config.write_text(original)
        _ = repo.git(repo.checkout, "commit", "-qam", "host configuration")
        _ = repo.git(repo.checkout, "push", "-q", "origin", "main")
        result = self.run_tune("example", host_key, "6", "--why", "Approved host capacity", "--user-words", "yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(repo.config.read_text(), original.replace(f"{host_key}=5", f"{host_key}=6"))
        self.assertEqual(repo.git(repo.checkout, "log", "-1", "--format=%s"), f"tune(example): {host_key} 5 -> 6")
        self.assertEqual(repo.head(), repo.remote_tip())

    def test_base_key_refuses_another_hosts_override_before_forwarding_can_select_a_local_host(self) -> None:
        repo = self.repository
        _ = repo.config.write_text(repo.config.read_text() + "# whole: 1..10\namount.other-host=5\n")
        _ = repo.git(repo.checkout, "commit", "-qam", "other host configuration")
        _ = repo.git(repo.checkout, "push", "-q", "origin", "main")
        before = self.snapshot()
        result = self.run_tune("example", "amount", "6", "--why", "Approved host capacity", "--user-words", "yes")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("amount at line 2", result.stderr)
        self.assertIn("amount.other-host at line 6", result.stderr)
        self.assertFalse(repo.ssh_log.exists())

    def test_repeating_a_successful_change_is_also_a_no_write_rerun(self) -> None:
        arguments = ("example", "amount", "3", "--why", "Approved tuning", "--user-words", "yes")
        first = self.run_tune(*arguments)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        before = self.snapshot()
        ssh_before = self.repository.ssh_log.read_bytes()
        repeated = self.run_tune(*arguments)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(repeated.stdout, "already amount=3\n")
        self.assertEqual(self.snapshot(), before)
        ssh_after = self.repository.ssh_log.read_bytes()
        self.assertTrue(ssh_after.startswith(ssh_before))
        self.assertIn(b"git fetch -q --no-write-fetch-head", ssh_after[len(ssh_before):])

    def reject_push(self) -> Path:
        hook = self.repository.origin / "hooks/pre-receive"
        _ = hook.write_text("#!/bin/sh\necho fixture-rejected >&2\nexit 1\n")
        hook.chmod(0o755)
        return hook

    def test_same_value_rerun_publishes_a_previously_rejected_push_without_a_new_commit(self) -> None:
        repo = self.repository
        hook = self.reject_push()
        arguments = ("example", "amount", "3", "--why", "Approved tuning", "--user-words", "yes")
        first = self.run_tune(*arguments)
        self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertNotEqual(repo.remote_tip(), repo.head())
        self.assertEqual(repo.mac_pulls(), 0)
        self.assertFalse((repo.root / "state/mac.json").exists())
        head = repo.head()
        contents = repo.config.read_bytes(), repo.config.stat().st_mtime_ns
        hook.unlink()
        repeated = self.run_tune(*arguments)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(repeated.stdout, "already amount=3\n")
        self.assertEqual(repo.head(), head)
        self.assertEqual(repo.remote_tip(), head)
        self.assertEqual((repo.config.read_bytes(), repo.config.stat().st_mtime_ns), contents)
        self.assertIn("git fetch -q --no-write-fetch-head", repo.ssh_log.read_text())

    def test_same_value_rerun_reports_a_push_that_is_still_rejected(self) -> None:
        repo = self.repository
        _ = self.reject_push()
        arguments = ("example", "amount", "3", "--why", "Approved tuning", "--user-words", "yes")
        first = self.run_tune(*arguments)
        self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
        before = self.snapshot()
        repeated = self.run_tune(*arguments)
        self.assertNotEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertTrue(repeated.stdout.startswith("already amount=3\n"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(repo.mac_pulls(), 0)

    def test_same_value_rerun_completes_a_mac_pull_interrupted_after_push(self) -> None:
        repo = self.repository
        arguments = ["example", "amount", "3", "--why", "Approved tuning", "--user-words", "yes"]
        with mock.patch.dict(os.environ, repo.env, clear=True), \
                mock.patch.object(main_publish, "pull_mac", side_effect=KeyboardInterrupt), \
                redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            _ = tune.main(arguments)
        self.assertEqual(repo.head(), repo.remote_tip())
        self.assertEqual(repo.mac_pulls(), 0)
        self.assertFalse((repo.root / "state/mac.json").exists())
        before = self.snapshot()
        repeated = self.run_tune(*arguments)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(repeated.stdout, "already amount=3\n")
        self.assertEqual(self.snapshot(), before)
        self.assertIn("git fetch -q --no-write-fetch-head", repo.ssh_log.read_text())

    def test_same_value_rerun_retries_a_queued_mac_pull_and_clears_the_queue(self) -> None:
        repo = self.repository
        offline = repo.root / "OFFLINE"
        _ = offline.write_text("")
        arguments = ("example", "amount", "3", "--why", "Approved tuning", "--user-words", "yes")
        first = self.run_tune(*arguments)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue((repo.root / "state/mac.json").exists())
        before = self.snapshot()
        offline.unlink()
        repeated = self.run_tune(*arguments)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((repo.root / "state/mac.json").exists())

    def test_unrelated_unpushed_commit_refuses_the_push_without_pulling_or_queuing_the_mac(self) -> None:
        repo = self.repository
        _ = (repo.checkout / "other.txt").write_text("unrelated local commit\n")
        _ = repo.git(repo.checkout, "commit", "-qam", "unrelated work")
        remote = repo.remote_tip()
        head = repo.head()
        result = self.run_tune("example", "amount", "3", "--why", "Approved tuning", "--user-words", "yes")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("other commit", result.stderr)
        self.assertEqual(repo.head(), head)
        self.assertIn("amount=3", repo.config.read_text())
        self.assertEqual(repo.remote_tip(), remote)
        self.assertEqual(repo.mac_pulls(), 0)
        self.assertFalse((repo.root / "state/mac.json").exists())

    def test_network_timeouts_refuse_fetch_retain_failed_push_and_queue_stalled_mac_within_one_minute(self) -> None:
        repo = self.repository
        original_run = subprocess.run
        limits: dict[str, float] = {}
        stage = "fetch"

        def network(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            operation = command[3] if command[0] == "git" else "mac"
            if operation == stage and command[-1] != "rc=0":
                timeout = options.get("timeout")
                if not isinstance(timeout, (int, float)) or timeout <= 0:
                    self.fail(f"{operation} has no positive time limit")
                limits[operation] = timeout
                raise subprocess.TimeoutExpired(command, timeout, output=b"connected", stderr=b"stalled")
            return original_run(command, **options)  # pyright: ignore[reportArgumentType, reportCallIssue, reportUnknownVariableType]

        arguments = ["example", "amount", "3", "--why", "Approved tuning", "--user-words", "yes"]
        head, remote = repo.head(), repo.remote_tip()
        with mock.patch.dict(os.environ, repo.env, clear=True), \
                mock.patch.object(subprocess, "run", side_effect=network), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = tune.main(arguments)
        self.assertEqual(status, 2)
        self.assertEqual((repo.head(), repo.remote_tip()), (head, remote))
        self.assertIn("amount=3", repo.config.read_text())
        stage = "push"
        with mock.patch.dict(os.environ, repo.env, clear=True), \
                mock.patch.object(subprocess, "run", side_effect=network), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = tune.main(arguments)
        self.assertEqual(status, 2)
        self.assertNotEqual(repo.head(), repo.remote_tip())
        self.assertEqual(repo.mac_pulls(), 0)
        stage = "mac"
        with mock.patch.dict(os.environ, repo.env, clear=True), \
                mock.patch.object(subprocess, "run", side_effect=network), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = tune.main(arguments)
            pending = main_publish.read_queue()
        self.assertEqual(status, 0)
        self.assertEqual(repo.head(), repo.remote_tip())
        self.assertIsInstance(pending, main_publish.PendingMacPull)
        if not isinstance(pending, main_publish.PendingMacPull):
            self.fail("the stalled Mac pull was not queued")
        self.assertIn("timed out", pending.reason)
        self.assertIn("stalled", pending.reason)
        self.assertIn("connected", pending.reason)
        self.assertLess(sum(limits.values()), 60)
        self.assertEqual(set(limits), {"fetch", "push", "mac"})

    def test_asleep_mac_keeps_the_pushed_change_in_the_shared_zone_queue(self) -> None:
        repo = self.repository
        _ = (repo.root / "OFFLINE").write_text("")
        result = self.run_tune("example", "amount", "3", "--why", "Approved tuning", "--user-words", "yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(repo.remote_tip(), repo.head())
        queued_lines = [line for line in result.stdout.splitlines() if "queued" in line.lower()]
        self.assertEqual(len(queued_lines), 1, result.stdout)
        with mock.patch.dict(os.environ, repo.env, clear=True):
            pending = main_publish.read_queue()
            self.assertIsInstance(pending, main_publish.PendingMacPull)
            if not isinstance(pending, main_publish.PendingMacPull):
                self.fail("the pushed change was not queued")
            self.assertIn("example", pending.label + pending.text)
            self.assertIn("amount", pending.label + pending.text)
            self.assertIn("Mac asleep", pending.reason)

    def test_mac_forwards_the_exact_arguments_to_natedev_without_a_local_edit(self) -> None:
        repo = self.repository
        before = self.snapshot()
        arguments = ["example", "amount", "3", "--why", "Keep one worker's space", "--user-words", 'yes "3"']
        with mock.patch.dict(os.environ, repo.env, clear=True), \
                mock.patch("platform.system", return_value="Darwin"), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = tune.main(arguments)
        self.assertEqual(status, 0)
        self.assertEqual(self.snapshot(), before)
        sent = repo.ssh_log.read_text().splitlines()
        self.assertIn("natedev", sent)
        forwarded = shlex.split(sent[-1])
        self.assertEqual(forwarded[-len(arguments):], arguments)
        self.assertIn("tune.py", sent[-1])


if __name__ == "__main__":
    _ = unittest.main()

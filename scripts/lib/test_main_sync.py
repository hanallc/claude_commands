"""main_sync fast-forwards a main checkout to origin/main only when that loses nothing."""

from __future__ import annotations

import fcntl
import io
import os
import shutil
import socket
import sys
import subprocess
import tempfile
import unittest
import unittest.mock
from collections.abc import Generator
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import override

from scripts.lib import live_settings, main_sync, waiting_rows
from scripts.lib.git_test_support import fixture_git_environment
from scripts.lint.memory_test_support import write_config_fixture
from scripts.message import session_send
from scripts.agents import remote
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, isolated_staff_file
from scripts.production.test_production_doc import enrolled_document


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
                          check=True, capture_output=True, text=True).stdout.strip()


class MainSyncTests(unittest.TestCase):
    root: Path = Path()
    origin: Path = Path()
    writer: Path = Path()
    checkout: Path = Path()

    @override
    def setUp(self) -> None:
        _ = self.enterContext(redirect_stdout(io.StringIO()))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = fixture_git_environment(self.root)
        self.enterContext(unittest.mock.patch.dict(os.environ, {
            **environment, "HOME": str(self.root), "GIT_CONFIG_SYSTEM": os.devnull, main_sync.STATE_ENV: str(self.root / "state"),
        }, clear=True))
        self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        config = self.root / "config"
        config.mkdir()
        _ = (config / "send.conf").write_text("# range: 0.001..\nmain_sync_repeat_minutes=17\n")
        _ = write_config_fixture(config / "user_zone.conf")
        binaries = self.root / "bin"
        binaries.mkdir()
        _ = (binaries / "git").symlink_to(shutil.which("git") or "/usr/bin/git")
        warm_status = binaries / "github-warm-status"
        _ = warm_status.write_text("#!/bin/sh\nexit 1\n")
        warm_status.chmod(0o755)
        self.enterContext(unittest.mock.patch.dict(os.environ, {
            "CONF_DIR": str(config), "XDG_STATE_HOME": str(self.root / "xdg"),
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}))
        self.origin = self.root / "origin.git"
        _ = subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.writer = self.root / "writer"
        _ = subprocess.run(["git", "clone", "-q", str(self.origin), str(self.writer)], check=True, capture_output=True)
        _ = self.commit("a")
        self.checkout = self.root / "checkout"
        _ = subprocess.run(["git", "clone", "-q", str(self.origin), str(self.checkout)], check=True, capture_output=True)

    def commit(self, name: str) -> str:
        _ = (self.writer / name).write_text(name)
        _ = git(self.writer, "add", name)
        _ = git(self.writer, "commit", "-q", "-m", name)
        _ = git(self.writer, "push", "-q", "origin", "HEAD:main")
        return git(self.writer, "rev-parse", "HEAD")

    def test_it_fast_forwards_and_leaves_untracked_files(self) -> None:
        self.assertEqual(main_sync.sync(self.checkout).state, main_sync.CURRENT)
        tip = self.commit("b")
        _ = (self.checkout / "session-state.json").write_text("{}")
        self.assertEqual(main_sync.sync(self.checkout).state, main_sync.ADVANCED)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), tip)
        self.assertTrue((self.checkout / "session-state.json").exists())

        _ = (self.writer / "a").write_text("updated a")
        _ = git(self.writer, "add", "a")
        _ = git(self.writer, "rm", "b")
        tip = self.commit("c")
        _ = (self.checkout / "a").write_text("updated a")
        _ = (self.checkout / "c").write_text("c")
        before = git(self.checkout, "rev-parse", "HEAD")
        outcome = main_sync.sync(self.checkout)
        self.assertEqual(outcome.state, main_sync.ADVANCED)
        self.assertIn(f"{before[:8]}..{tip[:8]}", outcome.detail)
        self.assertIn("finished an interrupted update", outcome.detail)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), tip)
        self.assertEqual(git(self.checkout, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual((self.checkout / "a").read_text(), "updated a")
        self.assertEqual((self.checkout / "c").read_text(), "c")
        self.assertEqual((self.checkout / "session-state.json").read_text(), "{}")

        with self.subTest(case="retry after restore"):
            _ = (self.writer / "a").write_text("next a")
            _ = git(self.writer, "add", "a")
            tip = self.commit("new-file")
            _ = (self.checkout / "new-file").write_text("new-file")
            self.assertEqual(git(self.checkout, "status", "--porcelain", "--untracked-files=no"), "")
            outcome = main_sync.sync(self.checkout)
            self.assertEqual(outcome.state, main_sync.ADVANCED)
            self.assertIn("finished an interrupted update", outcome.detail)
            self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), tip)
            self.assertEqual((self.checkout / "a").read_text(), "next a")
            self.assertEqual((self.checkout / "new-file").read_text(), "new-file")
            self.assertEqual((self.checkout / "session-state.json").read_text(), "{}")

        with self.subTest(case="directory becomes a file"):
            (self.writer / "d").mkdir()
            _ = self.commit("d/a")
            _ = git(self.checkout, "fetch", "-q", "origin")
            _ = git(self.checkout, "merge", "--ff-only", "-q", "origin/main")
            _ = git(self.writer, "rm", "d/a")
            tip = self.commit("d")
            (self.checkout / "d/a").unlink()
            (self.checkout / "d").rmdir()
            _ = (self.checkout / "d").write_text("d")
            outcome = main_sync.sync(self.checkout)
            self.assertEqual(outcome.state, main_sync.ADVANCED)
            self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), tip)
            self.assertEqual(git(self.checkout, "status", "--porcelain", "--untracked-files=no"), "")
            self.assertEqual((self.checkout / "d").read_text(), "d")
            self.assertEqual((self.checkout / "session-state.json").read_text(), "{}")

        with self.subTest(case="another fetch writes FETCH_HEAD"):
            _ = git(self.writer, "switch", "-qc", "topic")
            _ = (self.writer / "topic-only").write_text("Topic work")
            _ = git(self.writer, "add", "topic-only")
            _ = git(self.writer, "commit", "-qm", "Topic work")
            topic = git(self.writer, "rev-parse", "HEAD")
            _ = git(self.writer, "push", "-q", "origin", "topic")
            _ = git(self.writer, "switch", "-q", "main")
            tip = self.commit("main-only")
            lock = waiting_rows.locked_journal
            acquired = 0

            @contextmanager
            def fetch_topic(path: Path) -> Generator[waiting_rows.LockedJournal]:
                nonlocal acquired
                acquired += 1
                if acquired == 2:
                    _ = git(self.checkout, "fetch", "-q", "--refmap=", "origin", "refs/heads/topic")
                with lock(path) as pending:
                    yield pending

            with unittest.mock.patch.object(waiting_rows, "locked_journal", side_effect=fetch_topic):
                self.assertEqual(main_sync.sync(self.checkout).state, main_sync.ADVANCED)
            self.assertEqual(git(self.checkout, "rev-parse", "FETCH_HEAD"), topic)
            self.assertEqual(git(self.checkout, "rev-parse", "HEAD", "origin/main").splitlines(), [tip, tip])
            self.assertEqual(git(self.checkout, "for-each-ref", "refs/main-sync"), "")

    def test_an_advanced_checkout_cannot_move_back_during_claude_sync(self) -> None:
        base = git(self.checkout, "rev-parse", "HEAD")
        older = self.commit("b")
        newer = self.commit("c")
        _ = git(self.checkout, "fetch", "-q", "origin")
        _ = git(self.writer, "push", "-q", "--force", "origin", f"{older}:main")
        flock = fcntl.flock
        for case, start, concurrent, expected, tip in (
                ("behind origin", base, False, main_sync.ADVANCED, older),
                ("ahead of origin", newer, False, "diverged", newer),
                ("concurrent advance", base, True, "diverged", newer)):
            with self.subTest(case=case):
                _ = git(self.checkout, "reset", "--hard", start)
                _ = git(self.checkout, "update-ref", "refs/remotes/origin/main", newer)

                def finish_newer_pull(descriptor: int, operation: int) -> None:
                    if operation == fcntl.LOCK_EX and concurrent:
                        _ = git(self.checkout, "merge", "--ff-only", newer)
                    flock(descriptor, operation)

                with unittest.mock.patch.object(fcntl, "flock", finish_newer_pull):
                    self.assertEqual(main_sync.sync(self.checkout).state, expected)
                self.assertEqual(git(self.checkout, "rev-parse", "HEAD", "origin/main").splitlines(), [tip, tip])

        run_git = main_sync._git  # pyright: ignore[reportPrivateUsage]
        for race, expected in (("after fetch", "diverged"), ("before update-ref", "fetch-failed")):
            with self.subTest(race=race):
                _ = self.commit(f"old {race}")
                new_tip = ""

                def another_sync(checkout: Path, *arguments: str,
                                 timeout: float = 30) -> subprocess.CompletedProcess[str]:
                    nonlocal new_tip
                    fetch = race == "after fetch" and arguments[0] == "fetch"
                    replace_ref = race == "before update-ref" and arguments[:2] == ("update-ref", main_sync.TRACKING_REF)
                    if replace_ref:
                        new_tip = self.commit(f"new {race}")
                        _ = git(checkout, "fetch", "-q", "origin")
                        _ = git(checkout, "merge", "--ff-only", "-q", new_tip)
                    result = run_git(checkout, *arguments, timeout=timeout)
                    if fetch:
                        new_tip = self.commit(f"new {race}")
                        _ = git(checkout, "fetch", "-q", "origin")
                        _ = git(checkout, "merge", "--ff-only", "-q", new_tip)
                    return result

                with unittest.mock.patch.object(main_sync, "_git", side_effect=another_sync):
                    self.assertEqual(main_sync.sync(self.checkout).state, expected)
                self.assertTrue(new_tip)
                self.assertEqual(git(self.checkout, "rev-parse", "HEAD", "origin/main").splitlines(), [new_tip, new_tip])

        with self.subTest(race="origin replaces an unmerged tip"):
            local = git(self.checkout, "rev-parse", "HEAD")
            replaced = self.commit("replaced")
            _ = git(self.checkout, "fetch", "-q", "origin")
            self.assertEqual(git(self.checkout, "rev-parse", "origin/main"), replaced)
            _ = git(self.writer, "reset", "--hard", local)
            _ = (self.writer / "replacement").write_text("new origin tip")
            _ = git(self.writer, "add", "replacement")
            _ = git(self.writer, "commit", "-qm", "Replace the unmerged tip")
            replacement = git(self.writer, "rev-parse", "HEAD")
            _ = git(self.writer, "push", "--force", "origin", "HEAD:main")
            self.assertEqual(main_sync.sync(self.checkout).state, main_sync.ADVANCED)
            self.assertEqual(git(self.checkout, "rev-parse", "HEAD", "origin/main").splitlines(),
                             [replacement, replacement])

    def test_claude_sync_restores_the_accepted_save_when_head_is_current(self) -> None:
        (self.writer / "config").mkdir()
        _ = (self.writer / "config/example.conf").write_text("# whole: 1..\namount=2\n")
        _ = git(self.writer, "add", "config/example.conf")
        _ = self.commit("setting")
        self.assertEqual(main_sync.sync(self.checkout).state, main_sync.ADVANCED)
        path = self.checkout / "config/example.conf"
        with unittest.mock.patch.dict(os.environ, {"CONF_DIR": str(path.parent), "LIVE_SETTINGS_SAVE_ORDER": "1"}):
            self.assertEqual(live_settings.main(["example", "amount", "3"]), 0)
            _ = path.write_text("# whole: 1..\namount=2\n")
            self.assertEqual(main_sync.sync(self.checkout).state, main_sync.CURRENT)
        self.assertEqual(path.read_text(), "# whole: 1..\namount=3\n")

    def test_enrolled_live_files_refuse_incoming_entries_and_keep_refs(self) -> None:
        integration = self.root / "integration"
        _ = git(self.checkout, "worktree", "add", "-qb", "integration", str(integration))
        doc = self.checkout / "fixture-production-live.md"
        enrolled_document(doc, self.checkout, integration, "integration", log="live/log.md")
        log = self.checkout / "live/log.md"
        log.parent.mkdir()
        _ = log.write_text("current log\n")
        _ = fixture_member("showrunner", name="fixture", doc=staff.LiveDoc(remote.local_machine(), doc))
        refs = git(self.checkout, "for-each-ref", "--format=%(refname) %(objectname)")
        doc_bytes = doc.read_bytes()
        base = git(self.writer, "rev-parse", "HEAD")
        for incoming, contents in (("live/log.md", "current log\n"), ("live/log.md", "different\n"),
                                      ("live", "file in place of a directory\n"), ("live/log.md/entry", "entry\n")):
            with self.subTest(incoming=incoming, contents=contents):
                _ = git(self.writer, "reset", "--hard", base)
                path = self.writer / incoming
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_text(contents)
                _ = git(self.writer, "add", "--", incoming)
                _ = git(self.writer, "commit", "-qm", "Conflicting entry")
                _ = git(self.writer, "push", "-q", "--force", "origin", "HEAD:main")
                outcome = main_sync.sync(self.checkout)
                self.assertEqual(outcome.state, "live-file-conflict", outcome.detail)
                self.assertIn(str(log), outcome.detail)
                self.assertEqual((doc.read_bytes(), log.read_bytes()), (doc_bytes, b"current log\n"))
                self.assertEqual(git(self.checkout, "for-each-ref", "--format=%(refname) %(objectname)"), refs)

        _ = git(self.writer, "reset", "--hard", base)
        _ = git(self.writer, "push", "-q", "--force", "origin", "HEAD:main")
        tip = self.commit("unrelated")
        self.assertEqual(main_sync.sync(self.checkout).state, main_sync.ADVANCED)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), tip)
        self.assertEqual((doc.read_bytes(), log.read_bytes()), (doc_bytes, b"current log\n"))

        other_origin, other = self.root / "hana.git", self.root / "hana"
        _ = git(self.root, "clone", "-q", "--bare", str(self.writer), str(other_origin))
        _ = git(self.root, "clone", "-q", str(other_origin), str(other))
        hana_integration = self.root / "hana-integration"
        _ = git(other, "worktree", "add", "-qb", "init/catalyst", str(hana_integration))
        hana_doc = other / "docs/plans/hana-production-live.md"
        enrolled_document(hana_doc, other, hana_integration, "init/catalyst", log="docs/plans/hana-log.md")
        hana_log = other / "docs/plans/hana-log.md"
        _ = hana_log.write_text("hana log\n")
        _ = fixture_member("showrunner", name="hana", doc=staff.LiveDoc(remote.local_machine(), hana_doc))
        later = self.commit("hana-update")
        _ = git(self.writer, "push", "-q", str(other_origin), "HEAD:main")
        self.assertEqual(main_sync.sync(other).state, main_sync.ADVANCED)
        self.assertEqual(git(other, "rev-parse", "HEAD"), later)
        self.assertEqual(hana_log.read_bytes(), b"hana log\n")

    def test_each_blocked_case_leaves_the_checkout_alone(self) -> None:
        _ = self.commit("b")
        _ = git(self.checkout, "fetch", "-q", "origin")
        _ = git(self.checkout, "merge", "--ff-only", "-q", "origin/main")
        _ = self.commit("c")
        before = git(self.checkout, "rev-parse", "HEAD")

        with self.subTest(case="clean status with a blocked untracked addition"):
            _ = (self.checkout / "c").write_text("local addition")
            self.assertEqual(git(self.checkout, "status", "--porcelain", "--untracked-files=no"), "")
            outcome = main_sync.sync(self.checkout)
            self.assertEqual(outcome.state, "dirty")
            self.assertEqual(outcome.detail, f"{self.checkout} has local changes that block the update (c)")
            self.assertEqual((self.checkout / "c").read_text(), "local addition")
            (self.checkout / "c").unlink()

        with self.subTest(case="untracked file blocks an incoming directory"):
            (self.writer / "dir").mkdir()
            _ = self.commit("dir/file")
            _ = (self.checkout / "dir").write_text("local file")
            outcome = main_sync.sync(self.checkout)
            self.assertEqual(outcome.state, "dirty")
            self.assertEqual(outcome.detail, f"{self.checkout} has local changes that block the update (dir)")
            self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), before)
            self.assertEqual((self.checkout / "dir").read_text(), "local file")
            (self.checkout / "dir").unlink()

        _ = (self.checkout / "a").write_text("local edit")
        self.assertEqual(main_sync.sync(self.checkout).state, "dirty")
        self.assertEqual((self.checkout / "a").read_text(), "local edit")
        _ = (self.writer / "a").write_text("updated a")
        _ = git(self.writer, "add", "a")
        _ = self.commit("d")
        _ = (self.checkout / "a").write_text("updated a")
        _ = (self.checkout / "b").write_text("local edit")
        index_before = git(self.checkout, "ls-files", "--stage")
        self.assertEqual(main_sync.sync(self.checkout).state, "dirty")
        self.assertEqual((self.checkout / "a").read_text(), "updated a")
        self.assertEqual((self.checkout / "b").read_text(), "local edit")
        self.assertEqual(git(self.checkout, "ls-files", "--stage"), index_before)
        _ = git(self.checkout, "checkout", "-q", "b")

        with self.subTest(case="staged edit differs from both commits"):
            _ = (self.checkout / "a").write_text("staged edit")
            _ = git(self.checkout, "add", "a")
            _ = (self.checkout / "a").write_text("updated a")
            index_before = git(self.checkout, "ls-files", "--stage")
            self.assertEqual(main_sync.sync(self.checkout).state, "dirty")
            self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), before)
            self.assertEqual(git(self.checkout, "ls-files", "--stage"), index_before)
            self.assertEqual(git(self.checkout, "show", ":a"), "staged edit")
            self.assertEqual((self.checkout / "a").read_text(), "updated a")
            _ = git(self.checkout, "reset", "-q", "HEAD", "--", "a")

        with self.subTest(case="two blocked files and one matching file"):
            _ = (self.checkout / "b").write_text("local edit")
            _ = (self.checkout / "c").write_text("local addition")
            outcome = main_sync.sync(self.checkout)
            self.assertEqual(outcome.state, "dirty")
            self.assertEqual(outcome.detail, f"{self.checkout} has local changes that block the update (b, c)")
            _ = git(self.checkout, "checkout", "-q", "b")
            (self.checkout / "c").unlink()

        _ = git(self.checkout, "checkout", "-q", "a")
        _ = git(self.checkout, "commit", "-q", "--allow-empty", "-m", "local")
        self.assertEqual(main_sync.sync(self.checkout).state, "diverged")
        _ = git(self.checkout, "reset", "-q", "--hard", before)
        _ = git(self.checkout, "checkout", "-q", "-b", "other")
        self.assertEqual(main_sync.sync(self.checkout).state, "not-main")
        _ = git(self.checkout, "checkout", "-q", "main")
        _ = git(self.checkout, "remote", "set-url", "origin", str(self.root / "missing.git"))
        self.assertEqual(main_sync.sync(self.checkout).state, "fetch-failed")
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), before)

    def test_a_good_run_between_blocked_runs_writes_only_the_log(self) -> None:
        sent: list[str] = []

        def notify(_name: str, outcome: main_sync.Outcome) -> None:
            sent.append(outcome.state)

        with unittest.mock.patch.object(main_sync, "notify", notify):
            _ = self.commit("b")
            _ = (self.checkout / "a").write_text("local edit")
            self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 1)
            self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 1)
            self.assertEqual(sent, ["dirty", "dirty"])
            _ = git(self.checkout, "checkout", "-q", "a")
            self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 0)
            self.assertEqual(list((self.root / "state").iterdir()), [self.root / "state" / main_sync.SYNC_LOG])
            _ = self.commit("c")
            _ = (self.checkout / "b").write_text("edit again")
            self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 1)
        self.assertEqual(sent, ["dirty", "dirty", "dirty"])
        self.assertEqual(list((self.root / "state").iterdir()), [self.root / "state" / main_sync.SYNC_LOG])
        log = (self.root / "state" / main_sync.SYNC_LOG).read_text().splitlines()
        self.assertEqual(len(log), 4)

    def test_each_blocked_run_uses_the_host_state_key_and_current_config_window(self) -> None:
        with (unittest.mock.patch.object(main_sync, "sync", return_value=main_sync.Outcome("dirty", "local edit")),
              unittest.mock.patch.object(socket, "gethostname", return_value="fixture-host"),
              unittest.mock.patch.object(subprocess, "run") as send):
            for minutes in (17, 23):
                _ = (self.root / "config/send.conf").write_text(f"# range: 0.001..\nmain_sync_repeat_minutes={minutes}\n")
                self.assertEqual(main_sync.main(["--checkout", str(self.checkout), "--notify", "natedev"]), 1)
                send.assert_called_with(
                    [sys.executable, str(session_send.SEND_SCRIPT), "--to", "natedev", "--from", "main-sync",
                     "--text", "main sync on fixture-host: local edit. Commands there stay old until this is "
                     + "cleared; the job retries every 15 minutes.", "--key", "main-sync-fixture-host-dirty",
                     "--repeat-minutes", str(minutes)], check=False, capture_output=True)
            self.assertEqual(send.call_count, 2)

    def test_fetch_failure_advice_uses_the_credential_status(self) -> None:
        _ = git(self.checkout, "remote", "set-url", "origin", str(self.root / "missing.git"))
        status = self.root / "bin/github-warm-status"
        for code in (0, 1):
            with self.subTest(status=code):
                _ = status.write_text(f"#!/bin/sh\nexit {code}\n")
                outcome = main_sync.sync(self.checkout)
                self.assertEqual(outcome.state, "fetch-failed")
                if code == 0:
                    self.assertIn("GitHub credentials are warm", outcome.detail)
                    self.assertIn("GitHub refused", outcome.detail)
                    self.assertIn("likely a GitHub incident", outcome.detail)
                    self.assertIn("the job retries", outcome.detail)
                    self.assertNotIn("github-warmup", outcome.detail)
                else:
                    self.assertIn("run github-warmup at a terminal on", outcome.detail)
                    self.assertNotIn("credentials are warm", outcome.detail)
        status.unlink()
        with unittest.mock.patch.dict(os.environ, {"PATH": str(self.root / "bin")}):
            outcome = main_sync.sync(self.checkout)
        self.assertEqual(outcome.state, "fetch-failed")
        self.assertIn("run github-warmup at a terminal on", outcome.detail)

    def test_a_credential_probe_timeout_uses_the_cold_advice(self) -> None:
        results = [subprocess.CompletedProcess(["git"], 0, "main\n", ""),
                   subprocess.CompletedProcess(["git"], 0, str(self.checkout / ".git"), ""),
                   subprocess.CompletedProcess(["git"], 1, "", ""),
                   subprocess.CompletedProcess(["git"], 1, "", "fetch refused"),
                   subprocess.CompletedProcess(["git"], 0, "", "")]
        with (unittest.mock.patch.object(main_sync, "_git", side_effect=results),
              unittest.mock.patch.object(waiting_rows, "restore_live"),
              unittest.mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(
                  ["github-warm-status"], 5)) as probe):
            outcome = main_sync.sync(self.checkout)
        probe.assert_called_once_with(["github-warm-status"], capture_output=True, check=False, timeout=5)
        self.assertEqual(outcome.state, "fetch-failed")
        self.assertIn("run github-warmup at a terminal on", outcome.detail)
        self.assertNotIn("credentials are warm", outcome.detail)


if __name__ == "__main__":
    _ = unittest.main()

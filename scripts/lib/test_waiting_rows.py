"""A checkout moves to a new main and keeps each live setting row that main does not have yet."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.lib import conf, live_settings, waiting_rows
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

SETTINGS = "# whole: 1..\namount=2\n# whole: 1..\nlimit=5\n"
REGISTRY = "[assignments]\ndirect=codex\nreview=gpt-a:high\n"
STATUSLINE = "account.claude1=brown\n"


class WaitingRowsTests(unittest.TestCase):
    root: Path = Path()
    checkout: Path = Path()
    peer: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="waiting-rows-")))
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update(HOME=str(self.root), XDG_STATE_HOME=str(self.root / "state"),
                           GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Rows Test", GIT_AUTHOR_EMAIL="rows@example.invalid",
                           GIT_COMMITTER_NAME="Rows Test", GIT_COMMITTER_EMAIL="rows@example.invalid")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, environment, clear=True)))
        origin = self.root / "origin.git"
        self.run_git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.checkout, self.peer = self.root / "checkout", self.root / "peer"
        self.run_git(self.root, "clone", "-q", str(origin), str(self.checkout))
        (self.checkout / "config").mkdir()
        _ = (self.checkout / "config/example.conf").write_text(SETTINGS)
        _ = (self.checkout / "config/models.conf").write_text(REGISTRY)
        _ = (self.checkout / "config/statusline.conf").write_text(STATUSLINE)
        self.run_git(self.checkout, "add", ".")
        self.run_git(self.checkout, "commit", "-qm", "start")
        self.run_git(self.checkout, "push", "-q", "origin", "HEAD:main")
        self.run_git(self.root, "clone", "-q", str(origin), str(self.peer))

    def run_git(self, folder: Path, *arguments: str) -> None:
        _ = subprocess.run(["git", "-C", str(folder), *arguments], check=True, capture_output=True,
                           timeout=TEST_HANG_GUARD_SECS)

    def output(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.checkout), *arguments], check=True, capture_output=True,
                              text=True, timeout=TEST_HANG_GUARD_SECS).stdout

    def peer_change(self, path: str, old: str, new: str) -> str:
        file = self.peer / path
        _ = file.write_text(file.read_text().replace(old, new))
        self.run_git(self.peer, "commit", "-qam", f"peer {new}")
        self.run_git(self.peer, "push", "-q", "origin", "main")
        self.run_git(self.checkout, "fetch", "-q", "--no-write-fetch-head", "origin")
        return self.output("rev-parse", "origin/main").strip()

    def edit(self, path: str, old: str, new: str) -> None:
        file = self.checkout / path
        _ = file.write_text(file.read_text().replace(old, new))

    def test_replace_value_preserves_only_whitespace_comments(self) -> None:
        for original, expected in (
            ("account.claude1=brown\n", "account.claude1=#abcdef\n"),
            ("account.claude1=#123456\n", "account.claude1=#abcdef\n"),
            ("account.claude1=#123456 # comment\n", "account.claude1=#abcdef # comment\n"),
            ("account.claude1=#123456\t# comment\r\n", "account.claude1=#abcdef\t# comment\r\n"),
            (" account.claude1 =#123456  # note#fragment\n",
             " account.claude1 =#abcdef  # note#fragment\n"),
            ("account.claude1=  brown  # comment\n", "account.claude1=#abcdef  # comment\n"),
            ("account.claude1=part#fragment", "account.claude1=#abcdef"),
            ("account.claude1= # comment\n", "account.claude1=#abcdef # comment\n"),
        ):
            with self.subTest(original=original):
                source = "# heading\n" + original
                self.assertEqual(waiting_rows.replace_value(source, 1, "#abcdef"),
                                 "# heading\n" + expected)
                self.assertEqual(conf.settings(expected)["account.claude1"].value, "#abcdef")

    def test_a_waiting_value_stays_live_through_a_main_change_to_the_same_file(self) -> None:
        self.edit("config/example.conf", "amount=2", "amount=3")
        target = self.peer_change("config/example.conf", "limit=5", "limit=7")
        waiting_rows.move_to(self.checkout, "origin/main")
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), target)
        self.assertEqual((self.checkout / "config/example.conf").read_text(),
                         SETTINGS.replace("amount=2", "amount=3").replace("limit=5", "limit=7"))
        self.assertEqual(self.output("diff", "--cached", "--name-only", "HEAD"), "")
        self.assertEqual(self.output("diff", "--name-only", "HEAD"), "config/example.conf\n")

    def test_new_model_colors_stay_live_through_a_main_change_to_statusline(self) -> None:
        catalog = self.root / ".claude/cache/model-catalog/fixture-cc.json"
        catalog.parent.mkdir(parents=True)
        _ = catalog.write_text(json.dumps({"catalog": {"config": {"models": [{"id": "claude-opus-5-5"}]}}}))
        path = "config/statusline.conf"
        published = "model.gpt-6.2-sol=#123456\n"
        pending = "model.claude-opus-5-5=#abcdef\n"
        with mock.patch.dict(os.environ, {"CONF_DIR": str(self.checkout / "config")}):
            self.assertEqual(live_settings.main(["statusline", "model.claude-opus-5-5", "#abcdef"]), 0)
        self.edit(path, STATUSLINE + pending, STATUSLINE + pending + published)
        catalog.unlink()
        target = self.peer_change(path, STATUSLINE, "account.claude1=yellow\n" + published)
        waiting_rows.move_to(self.checkout, "origin/main")
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), target)
        self.assertEqual((self.checkout / path).read_text(), "account.claude1=yellow\n" + published + pending)
        self.assertEqual(self.output("diff", "--cached", "--name-only", "HEAD"), "")

    def test_other_new_keys_refuse_the_move_and_change_nothing(self) -> None:
        path = "config/statusline.conf"
        _ = self.peer_change(path, STATUSLINE, "account.claude1=yellow\n")
        head, index = self.output("rev-parse", "HEAD"), (self.checkout / ".git/index").read_bytes()
        for added in ("account.claude2=#abcdef\n", "model.=#abcdef\n", "model.gpt-6.2-sol=blue\n",
                      "model.gpt-6.2-sol=#abcdef\n",
                      "model.sol=#abcdef\n", "model.unknown=#abcdef\n", "model.claude-fixture-1=#abcdef\n"):
            with self.subTest(added=added):
                text = STATUSLINE + added
                _ = (self.checkout / path).write_text(text)
                with self.assertRaisesRegex(OSError, "local edits other than setting values"):
                    waiting_rows.move_to(self.checkout, "origin/main")
                self.assertEqual(self.output("rev-parse", "HEAD"), head)
                self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
                self.assertEqual((self.checkout / path).read_text(), text)

    def test_a_waiting_registry_row_stays_live_through_a_main_change_to_the_registry(self) -> None:
        self.edit("config/models.conf", "direct=codex", "direct=claude")
        target = self.peer_change("config/models.conf", "review=gpt-a:high", "review=gpt-b:max")
        waiting_rows.move_to(self.checkout, "origin/main")
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), target)
        self.assertEqual((self.checkout / "config/models.conf").read_text(),
                         REGISTRY.replace("direct=codex", "direct=claude").replace("gpt-a:high", "gpt-b:max"))
        self.assertEqual(self.output("diff", "--cached", "--name-only", "HEAD"), "")

    def test_a_local_edit_that_is_not_a_value_refuses_the_move_and_changes_nothing(self) -> None:
        self.edit("config/example.conf", "amount=2\n", "amount=3\n# a local note\n")
        _ = self.peer_change("config/example.conf", "limit=5", "limit=7")
        head, index = self.output("rev-parse", "HEAD"), (self.checkout / ".git/index").read_bytes()
        text = (self.checkout / "config/example.conf").read_text()
        with self.assertRaisesRegex(OSError, "local edits other than setting values"):
            waiting_rows.move_to(self.checkout, "origin/main")
        self.assertEqual(self.output("rev-parse", "HEAD"), head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual((self.checkout / "config/example.conf").read_text(), text)

    def test_a_local_edit_outside_the_settings_refuses_the_move_and_keeps_the_waiting_row(self) -> None:
        _ = (self.checkout / "notes.txt").write_text("local\n")
        self.run_git(self.checkout, "add", "notes.txt")
        self.run_git(self.checkout, "commit", "-qm", "notes")
        self.run_git(self.checkout, "push", "-q", "origin", "HEAD:main")
        self.run_git(self.peer, "pull", "-q", "--ff-only")
        _ = self.peer_change("notes.txt", "local", "peer")
        self.edit("config/example.conf", "amount=2", "amount=3")
        self.edit("notes.txt", "local", "edited")
        head = self.output("rev-parse", "HEAD")
        with self.assertRaises(OSError):
            waiting_rows.move_to(self.checkout, "origin/main")
        self.assertEqual(self.output("rev-parse", "HEAD"), head)
        self.assertIn("amount=3", (self.checkout / "config/example.conf").read_text())
        self.assertEqual(self.output("diff", "--cached", "--name-only", "HEAD"), "")

    def test_the_mac_command_refuses_a_target_that_is_not_a_fast_forward(self) -> None:
        self.run_git(self.checkout, "commit", "-q", "--allow-empty", "-m", "local only")
        _ = self.peer_change("config/example.conf", "limit=5", "limit=7")
        head = self.output("rev-parse", "HEAD")
        done = subprocess.run(["python3", str(Path(waiting_rows.__file__)), "fast-forward", "origin/main"],
                              cwd=self.checkout, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
                              check=False)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("is not a fast-forward of HEAD", done.stderr)
        self.assertEqual(self.output("rev-parse", "HEAD"), head)


if __name__ == "__main__":
    _ = unittest.main()

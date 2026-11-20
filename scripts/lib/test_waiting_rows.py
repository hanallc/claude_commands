"""A checkout moves to a new main and keeps each live setting row that main does not have yet."""
from __future__ import annotations

from contextlib import chdir, redirect_stderr
import fcntl
import io
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.lib.git_test_support import git_fixture_environment, write_fixture_git_config
from scripts.lib import conf, live_settings, main_publish, main_sync, waiting_rows
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.test_main_publish import pending_rows

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
        _ = self.enterContext(mock.patch.object(tempfile, "tempdir", str(self.root)))
        environment = git_fixture_environment()
        environment.update(HOME=str(self.root), XDG_STATE_HOME=str(self.root / "state"), TMPDIR=str(self.root),
                           GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(write_fixture_git_config(self.root)),
                           GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Rows Test", GIT_AUTHOR_EMAIL="rows@example.invalid",
                           GIT_COMMITTER_NAME="Rows Test", GIT_COMMITTER_EMAIL="rows@example.invalid")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, environment, clear=True)))
        origin = self.root / "origin.git"
        self.run_git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.checkout, self.peer = self.root / "checkout", self.root / "peer"
        os.environ["CONF_DIR"] = str(self.checkout / "config")
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

    def legacy_receipt(self, value: str) -> None:
        journal = waiting_rows.journal_path()
        journal.parent.mkdir(parents=True, exist_ok=True)
        _ = journal.write_text(json.dumps([{
            "stem": "statusline", "key": "account.claude1", "value": value,
            "message": "", "publish_on_main": False,
        }]))

    def peer_order(self, stem: str, key: str, order: int) -> None:
        _ = (self.peer / waiting_rows.ORDER_PATH).write_text(json.dumps({stem: {key: order}}) + "\n")
        self.run_git(self.peer, "add", waiting_rows.ORDER_PATH)

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

        path = "config/statusline.conf"
        self.edit(path, "brown", "#112233")
        self.legacy_receipt("#112233")
        target = self.peer_change(path, "brown", "#445566")
        waiting_rows.move_to(self.checkout, target)
        self.assertEqual((self.checkout / path).read_text(), "account.claude1=#445566\n")
        with pending_rows() as changes:
            self.assertEqual(changes, [])

    def test_an_advancing_pull_keeps_the_pending_values_inline_comment(self) -> None:
        relative = "config/example.conf"
        with mock.patch.dict(os.environ, {"LIVE_SETTINGS_SAVE_ORDER": "1"}):
            self.assertEqual(live_settings.main(["example", "amount", "3"]), 0)
        self.edit(relative, "amount=3", "amount=3 # local note")
        target = self.peer_change(relative, "limit=5", "limit=7")
        waiting_rows.move_to(self.checkout, target)
        self.assertEqual((self.checkout / relative).read_text(),
                         SETTINGS.replace("amount=2", "amount=3 # local note").replace("limit=5", "limit=7"))

    def test_a_pull_publishing_the_same_save_keeps_its_inline_comment(self) -> None:
        relative = "config/example.conf"
        with mock.patch.dict(os.environ, {"LIVE_SETTINGS_SAVE_ORDER": "1"}):
            self.assertEqual(live_settings.main(["example", "amount", "3"]), 0)
        self.edit(relative, "amount=3", "amount=3 # local note")
        self.peer_order("example", "amount", 1)
        target = self.peer_change(relative, "amount=2", "amount=3")
        waiting_rows.move_to(self.checkout, target)
        self.assertEqual((self.checkout / relative).read_text(), SETTINGS.replace("amount=2", "amount=3 # local note"))
        with waiting_rows.locked_journal(waiting_rows.journal_path()) as pending:
            self.assertEqual(pending.accepted["example"]["amount"], waiting_rows.AcceptedOrder(1))

    def test_an_order_only_pull_replaces_a_migrated_receipt(self) -> None:
        relative = "config/example.conf"
        journal = waiting_rows.journal_path()
        journal.parent.mkdir(parents=True, exist_ok=True)
        _ = journal.write_text(json.dumps([{"stem": "example", "key": "amount", "value": "3",
                                            "message": "", "publish_on_main": False}]))
        self.edit(relative, "amount=2", "amount=3")
        waiting_rows.move_to(self.checkout, "HEAD")
        self.peer_order("example", "amount", 2)
        target = self.peer_change(relative, "amount=2", "amount=2")
        waiting_rows.move_to(self.checkout, target)
        self.assertEqual((self.checkout / relative).read_text(), SETTINGS)
        with waiting_rows.locked_journal(journal) as pending:
            self.assertEqual(pending.accepted["example"]["amount"], waiting_rows.AcceptedOrder(2))

    def test_a_staged_setting_refuses_before_the_plan_is_written(self) -> None:
        relative = "config/example.conf"
        self.edit(relative, "limit=5", "limit=8")
        self.run_git(self.checkout, "add", relative)
        self.edit(relative, "limit=8", "limit=5")
        self.edit(relative, "amount=2", "amount=3")
        target = self.peer_change(relative, "limit=5", "limit=7")
        head = self.output("rev-parse", "HEAD")
        with self.assertRaisesRegex(OSError, f"staged settings prevent the move: {relative}"):
            waiting_rows.move_to(self.checkout, target)
        self.assertFalse(waiting_rows.move_marker(self.checkout).exists())
        self.assertEqual(self.output("rev-parse", "HEAD"), head)
        self.assertEqual(self.output("show", f":{relative}"), SETTINGS.replace("limit=5", "limit=8"))
        self.assertEqual((self.checkout / relative).read_text(), SETTINGS.replace("amount=2", "amount=3"))

    def test_a_save_cannot_enter_between_capture_and_restore(self) -> None:
        path = "config/statusline.conf"
        self.edit(path, STATUSLINE, "account.claude1=#112233\n")
        target = self.peer_change(path, STATUSLINE, STATUSLINE + "account.claude2=yellow\n")
        journal_lock = waiting_rows.journal_path().with_suffix(".lock")
        journal_lock.parent.mkdir(parents=True, exist_ok=True)
        checkout_lock = waiting_rows.move_marker(self.checkout).with_suffix(".lock")
        give = waiting_rows.give

        def check_locks(checkout: Path, rows: waiting_rows.WaitingRows,
                        environment: dict[str, str] | None = None) -> None:
            with checkout_lock.open("a") as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with journal_lock.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            give(checkout, rows, environment)

        with mock.patch.object(waiting_rows, "give", check_locks):
            waiting_rows.move_to(self.checkout, target)
        self.assertEqual(live_settings.main(["statusline", "account.claude1", "#445566"]), 0)
        self.assertEqual((self.checkout / path).read_text(), "account.claude1=#445566\naccount.claude2=yellow\n")
        with pending_rows() as changes:
            self.assertEqual([change.value for change in changes], ["#445566"])
        target = self.peer_change(path, "brown", "#445566")
        waiting_rows.move_to(self.checkout, target)
        with pending_rows() as changes:
            self.assertEqual(changes, [])

    def test_move_restores_a_new_save_that_arrives_after_preflight(self) -> None:
        relative = "config/example.conf"
        self.edit(relative, "amount=2", "amount=3")
        target = self.peer_change(relative, "limit=5", "limit=7")
        give = waiting_rows.give
        saved: list[live_settings.StagedSave | live_settings.UnchangedSave] = []

        def save_during_move(checkout: Path, rows: waiting_rows.WaitingRows,
                             environment: dict[str, str] | None = None) -> None:
            if environment is None:
                saved.append(live_settings.stage_save(live_settings.WaitingChange(
                    "example", "amount", "4", "tune(example): change amount")))
            give(checkout, rows, environment)

        with mock.patch.object(waiting_rows, "give", save_during_move):
            waiting_rows.move_to(self.checkout, target)
        self.assertEqual(len(saved), 1)
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), target)
        self.assertEqual((self.checkout / relative).read_text(),
                         SETTINGS.replace("amount=2", "amount=4").replace("limit=5", "limit=7"))

    def test_a_json_save_after_preflight_keeps_the_incoming_sibling(self) -> None:
        name = waiting_rows.SETTINGS_JSON
        live = self.checkout / name
        _ = live.write_text('{"local": 1, "incoming": 1}\n')
        self.run_git(self.checkout, "add", name)
        self.run_git(self.checkout, "commit", "-qm", "Add settings")
        self.run_git(self.checkout, "push", "-q", "origin", "main")
        self.run_git(self.peer, "pull", "-q", "--ff-only")
        target = self.peer_change(name, '"incoming": 1', '"incoming": 2')
        with waiting_rows.locked_checkout(self.checkout):
            rows = waiting_rows.check_move(self.checkout, waiting_rows.move_target(
                self.checkout, target, fast_forward=True))
            _ = live.write_text('{"local": 2, "incoming": 1}\n')
            with mock.patch.object(live_settings, "publish", return_value=[]), \
                    mock.patch.object(main_publish, "ssh", return_value=subprocess.CompletedProcess([], 0)):
                self.assertEqual(live_settings.main(["publish-json", "--message", "Set local"]), 0)
            waiting_rows.apply_move(self.checkout, rows)
        self.assertEqual(cast(object, json.loads(live.read_text())), {"local": 2, "incoming": 2})
        self.assertFalse(waiting_rows.move_marker(self.checkout).exists())

    def test_the_registry_fast_forward_restores_a_save_after_its_check(self) -> None:
        path = "config/statusline.conf"
        self.edit(path, "brown", "#112233")
        target = self.peer_change(path, STATUSLINE, "account.claude1=#445566\naccount.claude2=yellow\n")
        with waiting_rows.locked_checkout(self.checkout):
            move = waiting_rows.move_target(self.checkout, target, fast_forward=True)
            rows = waiting_rows.check_move(self.checkout, move, registry_move=True)
            waiting_rows.apply_move(self.checkout, rows)
        self.assertEqual(live_settings.main(["statusline", "account.claude1", "#667788"]), 0)
        self.assertEqual((self.checkout / path).read_text(), "account.claude1=#667788\naccount.claude2=yellow\n")
        target = self.peer_change(path, "#445566", "#667788")
        waiting_rows.move_to(self.checkout, target)
        with pending_rows() as changes:
            self.assertEqual(changes, [])

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

    def test_a_matching_new_model_color_without_a_receipt_allows_an_advancing_pull(self) -> None:
        path = "config/statusline.conf"
        with_color = STATUSLINE + "model.gpt-6.2-sol=#abcdef\n"
        self.edit(path, STATUSLINE, with_color)
        self.assertFalse(waiting_rows.journal_path().exists())
        target = self.peer_change(path, STATUSLINE, with_color)
        waiting_rows.move_to(self.checkout, target)
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), target)
        self.assertEqual((self.checkout / path).read_text(), with_color)
        self.assertEqual(self.output("diff", "HEAD", "--", path), "")

    def test_a_matching_mac_receipt_clears_on_a_repeat_pull(self) -> None:
        target = self.output("rev-parse", "HEAD").strip()
        self.legacy_receipt("brown")
        waiting_rows.move_to(self.checkout, target)
        with pending_rows() as changes:
            self.assertEqual(changes, [])

    def test_a_failed_restore_finishes_on_a_repeat_pull(self) -> None:
        path = "config/statusline.conf"
        with mock.patch.dict(os.environ, {"LIVE_SETTINGS_SAVE_ORDER": "1"}):
            self.assertEqual(live_settings.main(["statusline", "account.claude1", "#112233"]), 0)
        _ = (self.peer / waiting_rows.ORDER_PATH).write_text('{"statusline":{"account.claude1":2}}\n')
        self.run_git(self.peer, "add", waiting_rows.ORDER_PATH)
        target = self.peer_change(path, "brown", "#445566")

        def fail_restore(file: Path, text: str, *, new_file_mode: int | None = None) -> None:
            if file == self.checkout / path:
                raise OSError("fixture restore failed")
            write_text_atomic(file, text, new_file_mode=new_file_mode)

        with mock.patch.object(waiting_rows, "write_text_atomic", fail_restore):
            with self.assertRaisesRegex(OSError, "fixture restore failed"):
                waiting_rows.move_to(self.checkout, target)
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), target)
        self.assertEqual((self.checkout / path).read_text(), "account.claude1=#112233\n")
        waiting_rows.move_to(self.checkout, target)
        self.assertEqual((self.checkout / path).read_text(), "account.claude1=#445566\n")
        with pending_rows() as changes:
            self.assertEqual(changes, [])

    def test_a_repeat_pull_adds_the_key_from_a_failed_restore(self) -> None:
        relative = "config/example.conf"
        path = self.checkout / relative
        with mock.patch.dict(os.environ, {"LIVE_SETTINGS_SAVE_ORDER": "1"}):
            self.assertEqual(live_settings.main(["example", "amount", "3"]), 0)
        target = self.peer_change(relative, SETTINGS, SETTINGS + "extra=8\n")

        def fail_restore(file: Path, text: str, *, new_file_mode: int | None = None) -> None:
            if file == path:
                raise OSError("fixture restore failed")
            write_text_atomic(file, text, new_file_mode=new_file_mode)

        with mock.patch.object(waiting_rows, "write_text_atomic", fail_restore):
            with self.assertRaisesRegex(OSError, "fixture restore failed"):
                waiting_rows.move_to(self.checkout, target)
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), target)
        self.assertNotIn("extra", conf.settings(path.read_text()))
        waiting_rows.move_to(self.checkout, target)
        self.assertEqual(path.read_text(), SETTINGS.replace("amount=2", "amount=3") + "extra=8\n")
        with pending_rows() as changes:
            self.assertEqual([row.value for row in changes], ["3"])

    def test_a_current_pull_keeps_the_local_value_and_comment(self) -> None:
        relative = "config/example.conf"
        self.peer_order("example", "amount", 1)
        target = self.peer_change(relative, "amount=2", "amount=3")
        waiting_rows.move_to(self.checkout, target)
        expected = SETTINGS.replace("amount=2", "amount=3").replace("limit=5", "limit=7\n# local note")
        path = self.checkout / relative
        _ = path.write_text(expected)
        waiting_rows.move_to(self.checkout, target)
        self.assertEqual(path.read_text(), expected)

    def test_a_git_edit_at_the_same_order_reaches_existing_and_fresh_macs(self) -> None:
        relative = "config/example.conf"
        with mock.patch.dict(os.environ, {"LIVE_SETTINGS_SAVE_ORDER": "1"}):
            self.assertEqual(live_settings.main(["example", "amount", "3"]), 0)
        self.peer_order("example", "amount", 1)
        waiting_rows.move_to(self.checkout, self.peer_change(relative, "amount=2", "amount=3"))
        target = self.peer_change(relative, "amount=3", "amount=4")
        expected = SETTINGS.replace("amount=2", "amount=4")
        for checkout, state in ((self.checkout, "state"), (self.peer, "fresh-state")):
            with self.subTest(checkout=checkout), mock.patch.dict(os.environ, {
                "CONF_DIR": str(checkout / "config"), "XDG_STATE_HOME": str(self.root / state),
            }):
                waiting_rows.move_to(checkout, target)
                self.assertEqual((checkout / relative).read_text(), expected)
                with waiting_rows.locked_journal(waiting_rows.journal_path()) as pending:
                    self.assertEqual(pending.accepted["example"]["amount"], waiting_rows.AcceptedOrder(1))

    def test_a_published_setting_follows_deletion_from_git(self) -> None:
        for stem, key, value, before, published, edited in (
            ("statusline", "model.gpt-6.2-sol", "#abcdef", STATUSLINE,
             STATUSLINE + "model.gpt-6.2-sol=#abcdef\n", "#506070"),
            ("example", "amount", "3", SETTINGS, SETTINGS.replace("amount=2", "amount=3"), "8"),
        ):
            with self.subTest(key=key):
                relative = f"config/{stem}.conf"
                row = f"{key}={value}\n"
                self.peer_order(stem, key, 1)
                start = self.peer_change(relative, before, published)
                waiting_rows.move_to(self.checkout, start)
                self.run_git(self.peer, "rm", waiting_rows.ORDER_PATH)
                removed = self.peer_change(relative, row, "")
                waiting_rows.move_to(self.checkout, removed)
                self.assertNotIn(key, conf.settings((self.checkout / relative).read_text()))
                with waiting_rows.locked_journal(waiting_rows.journal_path()) as pending:
                    self.assertEqual(pending.accepted[stem][key], waiting_rows.AcceptedOrder(1))
                with mock.patch.dict(os.environ, {"LIVE_SETTINGS_SAVE_ORDER": "1"}):
                    self.assertEqual(live_settings.main([stem, key, value]), 0)
                self.assertNotIn(key, conf.settings((self.checkout / relative).read_text()))
                without = published.replace(row, "")
                new_row = f"{key}={edited}\n"
                restored = self.peer_change(relative, without, without + new_row)
                waiting_rows.move_to(self.checkout, restored)
                self.assertIn(new_row, (self.checkout / relative).read_text())

    def test_a_pending_value_survives_a_git_removal_until_its_order_publishes(self) -> None:
        relative = "config/example.conf"
        with mock.patch.dict(os.environ, {"LIVE_SETTINGS_SAVE_ORDER": "2"}):
            self.assertEqual(live_settings.main(["example", "amount", "3"]), 0)
        target = self.peer_change(relative, "amount=2\n", "")
        waiting_rows.move_to(self.checkout, target)
        with pending_rows() as changes:
            self.assertEqual([row.value for row in changes], ["3"])
        self.assertEqual(conf.settings((self.checkout / relative).read_text())["amount"].value, "3")
        self.peer_order("example", "amount", 2)
        published = self.peer_change(relative, "limit=5\n", "limit=5\namount=3\n")
        waiting_rows.move_to(self.checkout, published)
        with pending_rows() as changes:
            self.assertEqual(changes, [])
        self.assertEqual(self.output("status", "--porcelain"), "")

    def test_a_fresh_mac_rejects_an_older_save_for_a_deleted_color(self) -> None:
        path = "config/statusline.conf"
        key, value = "model.gpt-6.2-sol", "#abcdef"
        row = f"{key}={value}\n"
        self.peer_order("statusline", key, 1)
        _ = self.peer_change(path, STATUSLINE, STATUSLINE + row)
        self.peer_order("statusline", key, 2)
        target = self.peer_change(path, row, "")
        self.assertFalse(waiting_rows.journal_path().exists())
        waiting_rows.move_to(self.checkout, target)
        with mock.patch.dict(os.environ, {"LIVE_SETTINGS_SAVE_ORDER": "1"}):
            self.assertEqual(live_settings.main(["--accepted-model-color", "statusline", key, value]), 0)
        self.assertEqual((self.checkout / path).read_text(), STATUSLINE)
        with waiting_rows.locked_journal(waiting_rows.journal_path()) as journal:
            self.assertEqual(journal.accepted["statusline"][key], waiting_rows.AcceptedOrder(2))

    def test_a_failed_ref_update_leaves_the_move_ready_to_retry(self) -> None:
        path = self.checkout / "config/example.conf"
        target = self.peer_change("config/example.conf", "amount=2", "amount=3")
        head = self.output("rev-parse", "HEAD").strip()
        git = waiting_rows.git

        def fail_ref(checkout: Path, *arguments: str, input_text: str | None = None,
                     environment: dict[str, str] | None = None) -> str:
            if arguments[0] == "update-ref":
                self.assertEqual(path.read_text(), SETTINGS.replace("amount=2", "amount=3"))
                raise OSError("fixture ref update failed")
            return git(checkout, *arguments, input_text=input_text, environment=environment)

        with mock.patch.object(waiting_rows, "git", fail_ref):
            with self.assertRaisesRegex(OSError, "fixture ref update failed"):
                waiting_rows.move_to(self.checkout, target, fast_forward=True)
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), head)
        self.assertEqual(path.read_text(), SETTINGS)
        waiting_rows.move_to(self.checkout, target, fast_forward=True)
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), target)
        self.assertEqual(path.read_text(), SETTINGS.replace("amount=2", "amount=3"))

    def test_a_stopped_move_recovers_the_settings_and_order_file_before_the_next_pull(self) -> None:
        relative = "config/example.conf"
        key = "amount"
        head = self.output("rev-parse", "HEAD").strip()
        self.peer_order("example", key, 1)
        first = SETTINGS.replace("amount=2", "amount=3\n# peer comment")
        target = self.peer_change(relative, SETTINGS, first)
        stopped = self.stop_move(target, "read-tree")
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), head)
        self.assertEqual((self.checkout / relative).read_text(), first)
        self.assertIn(waiting_rows.ORDER_PATH, self.output("diff", "--cached", "--name-only"))
        self.peer_order("example", key, 2)
        second = SETTINGS.replace("amount=2", "amount=4")
        newest = self.peer_change(relative, first, second)
        waiting_rows.move_to(self.checkout, newest, fast_forward=True)
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), newest)
        self.assertEqual((self.checkout / relative).read_text(), second)
        self.assertEqual(self.output("status", "--porcelain"), "")
        self.assertFalse(waiting_rows.move_marker(self.checkout).exists())

    def stop_move(self, target: str, step: str) -> subprocess.CompletedProcess[bytes]:
        child = '''import os, signal, sys
from pathlib import Path
from scripts.lib import waiting_rows

git = waiting_rows.git
write = waiting_rows.write_text_atomic
step = sys.argv[2]
marker_writes = 0
def stop_at_git(checkout, *args, **kwargs):
    if args[:3] == ("read-tree", "-m", "-u") and not kwargs.get("environment") and step == "give":
        os.kill(os.getpid(), signal.SIGKILL)
    if args[0] == "update-ref" and step == "read-tree":
        os.kill(os.getpid(), signal.SIGKILL)
    result = git(checkout, *args, **kwargs)
    if args[0] == "update-ref" and step == "update-ref":
        os.kill(os.getpid(), signal.SIGKILL)
    return result

def stop_at_write(path, text, **kwargs):
    global marker_writes
    write(path, text, **kwargs)
    if path.name == "waiting-rows.move":
        marker_writes += 1
        if step == "marker" or (step == "before-give" and marker_writes == 2):
            os.kill(os.getpid(), signal.SIGKILL)

waiting_rows.git = stop_at_git
waiting_rows.write_text_atomic = stop_at_write
waiting_rows.move_to(Path.cwd(), sys.argv[1], fast_forward=True)
'''
        return subprocess.run([sys.executable, "-c", child, target, step], cwd=self.checkout, capture_output=True,
                              env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
                              check=False, timeout=TEST_HANG_GUARD_SECS)

    def test_a_stop_after_update_ref_restores_the_unregistered_value_and_incoming_key(self) -> None:
        relative = "config/example.conf"
        self.edit(relative, "amount=2", "amount=3")
        target = self.peer_change(relative, "limit=5", "limit=7")
        stopped = self.stop_move(target, "update-ref")
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), target)
        expected = SETTINGS.replace("amount=2", "amount=3")
        self.assertEqual((self.checkout / relative).read_text(), expected)
        waiting_rows.move_to(self.checkout, target, fast_forward=True)
        self.assertEqual((self.checkout / relative).read_text(), expected.replace("limit=5", "limit=7"))
        self.assertFalse(waiting_rows.move_marker(self.checkout).exists())

    def test_recovery_refuses_to_overwrite_an_edit_after_a_stop(self) -> None:
        relative = "config/example.conf"
        self.edit(relative, "amount=2", "amount=3")
        target = self.peer_change(relative, "limit=5", "limit=7")
        stopped = self.stop_move(target, "update-ref")
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        self.edit(relative, "amount=3", "amount=3 # newer edit")
        with self.assertRaisesRegex(OSError, f"{relative} changed after the move started"):
            waiting_rows.move_to(self.checkout, target, fast_forward=True)
        self.assertEqual((self.checkout / relative).read_text(), SETTINGS.replace("amount=2", "amount=3 # newer edit"))
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), target)
        self.assertTrue(waiting_rows.move_marker(self.checkout).exists())

    def test_pull_recovers_after_a_stop_before_a_failed_fetch(self) -> None:
        relative = "config/example.conf"
        path = self.checkout / relative
        recovery = waiting_rows.move_marker(self.checkout)
        binaries = self.root / "bin"
        binaries.mkdir()
        warm = binaries / "github-warm-status"
        _ = warm.write_text("#!/bin/sh\nexit 1\n")
        warm.chmod(0o755)
        os.environ["PATH"] = str(binaries) + os.pathsep + os.environ["PATH"]
        for pull in ("claude-sync", "Mac"):
            with self.subTest(pull=pull):
                if pull == "claude-sync":
                    target = self.peer_change(relative, "limit=5", "limit=7")
                else:
                    target = self.peer_change(relative, "limit=7", "limit=8")
                self.edit(relative, "amount=2", "amount=3")
                stopped = self.stop_move(target, "update-ref")
                self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
                self.assertTrue(recovery.exists())
                if pull == "claude-sync":
                    self.run_git(self.checkout, "remote", "set-url", "origin", str(self.root / "absent.git"))
                    self.assertEqual(main_sync.sync(self.checkout).state, "fetch-failed")
                    self.run_git(self.checkout, "remote", "set-url", "origin", str(self.root / "origin.git"))
                else:
                    scripts = self.checkout / "scripts/lib"
                    scripts.mkdir(parents=True)
                    (scripts / "waiting_rows.py").touch()
                    runner = scripts / "py"
                    source = shlex.quote(str(Path(waiting_rows.__file__).resolve()))
                    _ = runner.write_text("#!/bin/sh\nshift\n"
                                          + f"exec {shlex.quote(sys.executable)} {source} \"$@\"\n")
                    runner.chmod(0o755)
                    git = shutil.which("git")
                    assert git is not None
                    git_tool = binaries / "git"
                    _ = git_tool.write_text("#!/bin/sh\ncase \"$1\" in\n"
                                             + "ls-remote) printf '%s\\trefs/heads/main\\n' \"$FIXTURE_TIP\";;\n"
                                             + "fetch) [ ! -e \"$FIXTURE_PLAN\" ] && flock -n \"$FIXTURE_LOCK\" true || exit 8; "
                                             + "echo 'fixture fetch failed' >&2; exit 7;;\n"
                                             + f"*) exec {shlex.quote(git)} \"$@\";;\nesac\n")
                    git_tool.chmod(0o755)
                    command = main_publish.mac_pull_command(PurePosixPath(self.checkout), self.root / "absent.git")
                    environment = {**os.environ, "FIXTURE_TIP": target, "FIXTURE_PLAN": str(recovery),
                                   "FIXTURE_LOCK": str(waiting_rows.journal_path().with_suffix(".lock")),
                                   "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
                    done = subprocess.run(["sh", "-c", command], env=environment, capture_output=True, check=True,
                                          text=True, timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(done.stdout, "rc=7\n", done.stderr)
                self.assertFalse(recovery.exists())
                self.assertEqual(path.read_text(), self.output("show", f"HEAD:{relative}").replace("amount=2", "amount=3"))

    def test_a_stop_before_give_recovers_a_pending_file_absent_from_git(self) -> None:
        relative = "config/example.conf"
        with mock.patch.dict(os.environ, {"LIVE_SETTINGS_SAVE_ORDER": "1"}):
            self.assertEqual(live_settings.main(["example", "amount", "3"]), 0)
        self.run_git(self.peer, "rm", relative)
        removed = self.peer_change("config/statusline.conf", "brown", "yellow")
        waiting_rows.move_to(self.checkout, removed)
        file = self.checkout / relative
        self.assertEqual(file.read_text(), "amount=3\n")
        _ = (self.peer / relative).write_text(SETTINGS)
        self.run_git(self.peer, "add", relative)
        target = self.peer_change(relative, "limit=5", "limit=7")
        stopped = self.stop_move(target, "marker")
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), removed)
        self.assertEqual(self.output("ls-files", "--", relative), "")
        waiting_rows.move_to(self.checkout, target)
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), target)
        self.assertEqual(file.read_text(), SETTINGS.replace("amount=2", "amount=3").replace("limit=5", "limit=7"))
        self.assertFalse(waiting_rows.move_marker(self.checkout).exists())

    def test_recovery_keeps_a_setting_staged_after_the_marker_write(self) -> None:
        relative = "config/example.conf"
        file = self.checkout / relative
        head = self.output("rev-parse", "HEAD").strip()
        target = self.peer_change(relative, "amount=2", "amount=3")
        before = SETTINGS.replace("limit=5", "limit=6")
        _ = file.write_text(before)
        stopped = self.stop_move(target, "marker")
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        marker = waiting_rows.move_marker(self.checkout)
        self.assertTrue(marker.exists())
        staged = SETTINGS.replace("limit=5", "limit=8")
        _ = file.write_text(staged)
        self.run_git(self.checkout, "add", "--", relative)
        _ = file.write_text(before)

        waiting_rows.restore_live(self.checkout)

        self.assertFalse(marker.exists())
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), head)
        self.assertEqual(file.read_text(), before)
        self.assertEqual(self.output("show", f":{relative}"), staged)

    def test_recovery_keeps_a_nonoverlapping_staged_edit_after_git_changes_the_index(self) -> None:
        relative = "config/example.conf"
        file = self.checkout / relative
        head = self.output("rev-parse", "HEAD").strip()
        target = self.peer_change(relative, "amount=2", "amount=3")
        before = SETTINGS.replace("limit=5", "limit=6")
        _ = file.write_text(before)
        stopped = self.stop_move(target, "read-tree")
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        self.assertEqual(self.output("show", f":{relative}"), SETTINGS.replace("amount=2", "amount=3"))
        staged = SETTINGS.replace("limit=5", "limit=8")
        _ = file.write_text(staged)
        self.run_git(self.checkout, "add", "--", relative)
        _ = file.write_text(before)

        waiting_rows.restore_live(self.checkout)

        self.assertEqual(self.output("rev-parse", "HEAD").strip(), head)
        self.assertFalse(waiting_rows.move_marker(self.checkout).exists())
        self.assertEqual(file.read_text(), before)
        self.assertEqual(self.output("show", f":{relative}"), staged)

    def test_recovery_reports_an_overlapping_staged_edit_and_keeps_both_texts(self) -> None:
        relative = "config/example.conf"
        file = self.checkout / relative
        head = self.output("rev-parse", "HEAD").strip()
        target = self.peer_change(relative, "amount=2", "amount=3")
        before = SETTINGS.replace("limit=5", "limit=6")
        _ = file.write_text(before)
        stopped = self.stop_move(target, "read-tree")
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        marker = waiting_rows.move_marker(self.checkout)
        plan = marker.read_bytes()
        staged = before.replace("amount=2", "amount=8")
        _ = file.write_text(staged)
        self.run_git(self.checkout, "add", "--", relative)
        _ = file.write_text(before)
        errors = io.StringIO()

        with chdir(self.checkout), redirect_stderr(errors):
            self.assertEqual(waiting_rows.main(["fast-forward", target]), 1)

        self.assertEqual(errors.getvalue().splitlines(), [
            f"waiting_rows: {relative}: staged edit overlaps the interrupted move. "
            + f"Inspect it with git diff --cached -- {relative}",
        ])
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), head)
        self.assertEqual(marker.read_bytes(), plan)
        self.assertEqual(file.read_text(), before)
        self.assertEqual(self.output("show", f":{relative}"), staged)

    def test_recovery_keeps_a_matching_index_entry_without_a_recorded_write(self) -> None:
        relative = "config/example.conf"
        path = self.checkout / relative
        head = self.output("rev-parse", "HEAD")
        target = self.peer_change(relative, "amount=2", "amount=3")
        before = SETTINGS.replace("limit=5", "limit=6")
        _ = path.write_text(before)
        stopped = self.stop_move(target, "before-give")
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        marker = waiting_rows.move_marker(self.checkout)
        plan = marker.read_bytes()
        staged = SETTINGS.replace("amount=2", "amount=3")
        _ = path.write_text(staged)
        self.run_git(self.checkout, "add", "--", relative)
        _ = path.write_text(before)
        errors = io.StringIO()

        with chdir(self.checkout), redirect_stderr(errors):
            self.assertEqual(waiting_rows.main(["fast-forward", target]), 1)

        self.assertEqual(errors.getvalue().splitlines(), [
            f"waiting_rows: {relative}: the move did not record the index write. "
            + f"Inspect it with git diff --cached -- {relative}",
        ])
        self.assertEqual(marker.read_bytes(), plan)
        self.assertEqual(self.output("rev-parse", "HEAD"), head)
        self.assertEqual(self.output("show", f":{relative}"), staged)
        self.assertEqual(path.read_text(), before)

    def test_recovery_reverses_only_the_recorded_hunk_in_a_staged_file(self) -> None:
        relative = "config/example.conf"
        path = self.checkout / relative
        head = self.output("rev-parse", "HEAD")
        target = self.peer_change(relative, "amount=2", "amount=3")
        before = SETTINGS.replace("limit=5", "limit=6")
        _ = path.write_text(before)
        stopped = self.stop_move(target, "give")
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        given = SETTINGS.replace("amount=2", "amount=3")
        self.assertEqual(self.output("show", f":{relative}"), given)
        _ = path.write_text(given.replace("limit=5", "limit=8"))
        self.run_git(self.checkout, "add", "--", relative)
        _ = path.write_text(before)

        waiting_rows.restore_live(self.checkout)

        self.assertEqual(self.output("rev-parse", "HEAD"), head)
        self.assertEqual(self.output("show", f":{relative}"), SETTINGS.replace("limit=5", "limit=8"))
        self.assertEqual(path.read_text(), before)
        self.assertFalse(waiting_rows.move_marker(self.checkout).exists())

    def test_recovery_keeps_an_unstaged_edit_that_matches_the_planned_file(self) -> None:
        relative = "config/example.conf"
        path = self.checkout / relative
        head = self.output("rev-parse", "HEAD")
        target = self.peer_change(relative, "amount=2", "amount=3")
        before = SETTINGS.replace("limit=5", "limit=6")
        _ = path.write_text(before)
        stopped = self.stop_move(target, "give")
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        self.assertEqual(path.read_text(), before)
        marker = waiting_rows.move_marker(self.checkout)
        plan = marker.read_bytes()
        staged = self.output("show", f":{relative}")
        edited = before.replace("amount=2", "amount=3")
        _ = path.write_text(edited)
        errors = io.StringIO()

        with chdir(self.checkout), redirect_stderr(errors):
            self.assertEqual(waiting_rows.main(["fast-forward", target]), 1)

        self.assertEqual(errors.getvalue().splitlines(), [
            f"waiting_rows: {relative} changed after the move started. Inspect it with git diff HEAD -- {relative}",
        ])
        self.assertEqual(self.output("rev-parse", "HEAD"), head)
        self.assertEqual(marker.read_bytes(), plan)
        self.assertEqual(self.output("show", f":{relative}"), staged)
        self.assertEqual(path.read_text(), edited)

    def test_recovery_reports_the_command_when_an_overlap_is_also_in_the_worktree(self) -> None:
        relative = "config/example.conf"
        path = self.checkout / relative
        head = self.output("rev-parse", "HEAD")
        target = self.peer_change(relative, "amount=2", "amount=3")
        before = SETTINGS.replace("limit=5", "limit=6")
        _ = path.write_text(before)
        stopped = self.stop_move(target, "read-tree")
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        marker = waiting_rows.move_marker(self.checkout)
        plan = marker.read_bytes()
        edited = before.replace("amount=2", "amount=8")
        _ = path.write_text(edited)
        self.run_git(self.checkout, "add", "--", relative)
        errors = io.StringIO()

        with chdir(self.checkout), redirect_stderr(errors):
            self.assertEqual(waiting_rows.main(["fast-forward", target]), 1)

        self.assertEqual(errors.getvalue().splitlines(), [
            f"waiting_rows: {relative}: staged edit overlaps the interrupted move. "
            + f"Inspect it with git diff --cached -- {relative}",
        ])
        self.assertEqual(self.output("rev-parse", "HEAD"), head)
        self.assertEqual(marker.read_bytes(), plan)
        self.assertEqual(self.output("show", f":{relative}"), edited)
        self.assertEqual(path.read_text(), edited)

    def test_a_retry_reaches_the_target_with_the_separate_edit_still_staged(self) -> None:
        relative = "config/example.conf"
        path = self.checkout / relative
        head = self.output("rev-parse", "HEAD")
        target = self.peer_change(relative, "amount=2", "amount=3")
        stopped = self.stop_move(target, "marker")
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        staged = SETTINGS.replace("limit=5", "limit=8")
        _ = path.write_text(staged)
        self.run_git(self.checkout, "add", "--", relative)
        _ = path.write_text(SETTINGS)

        waiting_rows.restore_live(self.checkout)

        self.assertEqual(self.output("rev-parse", "HEAD"), head)
        self.assertEqual(self.output("show", f":{relative}"), staged)
        self.assertFalse(waiting_rows.move_marker(self.checkout).exists())

        waiting_rows.move_to(self.checkout, target, fast_forward=True)

        self.assertEqual(self.output("rev-parse", "HEAD").strip(), target)
        self.assertEqual(self.output("show", f":{relative}"), staged.replace("amount=2", "amount=3"))
        self.assertEqual(path.read_text(), SETTINGS.replace("amount=2", "amount=3"))
        self.assertFalse(waiting_rows.move_marker(self.checkout).exists())

    def test_a_save_validates_the_key_after_move_recovery(self) -> None:
        relative = "config/example.conf"
        target = self.peer_change(relative, "amount=2\n", "")
        stopped = self.stop_move(target, "read-tree")
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        validate = mock.Mock(wraps=live_settings.editable_setting)
        with mock.patch.object(live_settings, "editable_setting", validate):
            saved = live_settings.stage_save(live_settings.WaitingChange("example", "amount", "3", "fixture"))
        validate.assert_called_once_with("example", "amount", "3", accepted=False)
        self.assertEqual(saved, live_settings.StagedSave("2", 1))
        self.assertEqual((self.checkout / relative).read_text(), SETTINGS.replace("amount=2", "amount=3"))

    def test_a_queued_mac_pull_checks_ancestry_after_the_newer_pull(self) -> None:
        path = "config/example.conf"
        older = self.peer_change(path, "limit=5", "limit=7")
        newer = self.peer_change(path, "limit=7", "limit=8")
        flock = fcntl.flock

        def finish_newer_pull(descriptor: int, operation: int) -> None:
            if operation == fcntl.LOCK_EX:
                self.run_git(self.checkout, "merge", "--ff-only", newer)
            flock(descriptor, operation)

        errors = io.StringIO()
        with chdir(self.checkout), mock.patch.object(fcntl, "flock", finish_newer_pull), redirect_stderr(errors):
            self.assertEqual(waiting_rows.main(["fast-forward", older]), 1)
        self.assertIn("is not a fast-forward of HEAD", errors.getvalue())
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), newer)
        self.assertIn("limit=8", (self.checkout / path).read_text())

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

    def test_a_failed_models_restore_finishes_on_a_same_tip_pull(self) -> None:
        relative = "config/models.conf"
        path = self.checkout / relative
        local = REGISTRY.replace("direct=codex", "direct=claude")
        _ = path.write_text(local)
        target = self.peer_change(relative, "gpt-a:high", "gpt-b:max")

        def fail_restore(file: Path, text: str, *, new_file_mode: int | None = None) -> None:
            if file == path:
                raise OSError("fixture models restore failed")
            write_text_atomic(file, text, new_file_mode=new_file_mode)

        with mock.patch.object(waiting_rows, "write_text_atomic", fail_restore):
            with self.assertRaisesRegex(OSError, "fixture models restore failed"):
                waiting_rows.move_to(self.checkout, target)
        self.assertEqual(self.output("rev-parse", "HEAD").strip(), target)
        self.assertEqual(path.read_text(), local)
        waiting_rows.move_to(self.checkout, target)
        self.assertEqual(path.read_text(), local.replace("gpt-a:high", "gpt-b:max"))
        self.assertFalse(waiting_rows.move_marker(self.checkout).exists())

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

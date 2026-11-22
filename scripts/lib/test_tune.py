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

from scripts.lib import conf, live_settings, main_publish, models_registry, tune, waiting_rows
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.test_main_publish import PublicationTestCase, pending_rows

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

    def test_only_a_saved_guest_key_prints_the_hana_image_notice(self) -> None:
        for file, key, saved, expected in (
            ('hana_aws', 'host', live_settings.StagedSave('fixture-hana', 1), 1),
            ('build_targets', 'enabled.hana-aws', live_settings.StagedSave('off', 1), 1),
            ('hana_aws', 'spot_max_usd_per_hour', live_settings.StagedSave('0.25', 2), 0),
            ('build_targets', 'enabled.hana-aws', live_settings.UnchangedSave(), 0),
        ):
            source = self.repository.checkout / f'{file}.conf'
            with (self.subTest(key=key, saved=saved), mock.patch.object(conf, 'path', return_value=source),
                    mock.patch.object(main_publish, 'checkout_of', return_value=self.repository.checkout),
                    mock.patch.object(live_settings, 'stage_save', return_value=saved),
                    mock.patch.object(live_settings, 'publish', return_value=[]),
                    mock.patch.object(live_settings, 'start_readers', return_value=[]),
                    mock.patch.object(live_settings, 'write_mac', return_value='Mac updated'),
                    redirect_stdout(io.StringIO()) as output):
                args = tune.RequestedValueChange(file=file, key=key, value='on')
                self.assertEqual(tune.change(args), 0)
            self.assertEqual(output.getvalue().splitlines().count(
                'The Hana image is now stale. Build and register a current Hana image.'), expected)

    def test_invalid_file_key_value_or_empty_approval_refuses_without_changing_anything(self) -> None:
        cases = (
            ("missing", "amount", "3", "valid reason", "approved"),
            ("example", "missing", "3", "valid reason", "approved"),
            ("example", "amount", "0", "valid reason", "approved"),
            ("example", "amount", "11", "valid reason", "approved"),
            ("example", "amount", "bad", "valid reason", "approved"),
            ("example", "mode", "unknown", "valid reason", "approved"),
            ("example", "mode", "fast # comment", "valid reason", "approved"),
            ("example", "mode", "fast\t# comment", "valid reason", "approved"),
            ("example", "mode", " fast", "valid reason", "approved"),
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

    def test_machine_switch_save_edits_only_its_exact_key_and_checks_its_limit(self) -> None:
        repo = self.repository
        source = repo.config.with_name("build_targets.conf")
        original = "# one of: on, off\nenabled.mac=on\nenabled.mac.mac=on\nenabled.hana-aws=off\n"
        _ = source.write_text(original)
        _ = repo.git(repo.checkout, "add", "config/build_targets.conf")
        _ = repo.git(repo.checkout, "commit", "-qm", "Add fixture machine switches")
        _ = repo.git(repo.checkout, "push", "-q", "origin", "main")
        for value, status in (("invalid", 2), ("off", 0), ("on", 0)):
            with self.subTest(value=value):
                result = self.run_tune("build_targets", "enabled.mac", value,
                                       "--why", "Change placement", "--user-words", "yes")
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                if status == 2:
                    self.assertIn("expected one of on, off", result.stderr)
                    self.assertEqual(source.read_text(), original)
                else:
                    self.assertEqual(source.read_text(), original.replace("enabled.mac=on", f"enabled.mac={value}"))

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
                         "config/example.conf\nconfig/settings-orders.json")
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

    def test_two_hex_color_saves_publish_each_value_and_keep_the_comment(self) -> None:
        repo = self.repository
        color_file = repo.config.with_name("statusline.conf")
        _ = color_file.write_text("account.claude1=brown # account color\n")
        _ = repo.git(repo.checkout, "add", "config/statusline.conf")
        _ = repo.git(repo.checkout, "commit", "-qm", "account color fixture")
        _ = repo.git(repo.checkout, "push", "-q", "origin", "main")
        previous_value = "brown"
        for value in ("#123456", "#abcdef"):
            with self.subTest(value=value):
                previous_commit = repo.head()
                result = self.run_tune("statusline", "account.claude1", value, "--why", "Chosen account color",
                                       "--user-words", f"Save claude1 color {value}")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                expected = f"account.claude1={value} # account color\n"
                self.assertEqual(color_file.read_text(), expected)
                self.assertNotEqual(repo.head(), previous_commit)
                self.assertEqual(repo.git(repo.checkout, "rev-parse", "HEAD^"), previous_commit)
                self.assertEqual(repo.remote_tip(), repo.head())
                self.assertEqual(repo.git(repo.origin, "show", "main:config/statusline.conf"), expected.strip())
                self.assertEqual(repo.git(repo.checkout, "status", "--porcelain=v1"), "")
                self.assertEqual(repo.git(repo.checkout, "log", "-1", "--format=%s"),
                                 f"tune(statusline): account.claude1 {previous_value} -> {value}")
                previous_value = value

    def test_only_a_missing_model_color_can_be_added_and_published(self) -> None:
        repo = self.repository
        color_file = repo.config.with_name("statusline.conf")
        original = "account.claude1=brown # account color"
        _ = color_file.write_text(original)
        _ = repo.git(repo.checkout, "add", "config/statusline.conf")
        _ = repo.git(repo.checkout, "commit", "-qm", "color fixture")
        _ = repo.git(repo.checkout, "push", "-q", "origin", "main")
        key = "model.gpt-6.2-sol"
        value = "#304050"
        approval = ("--why", "Chosen model color", "--user-words", "Save the model color")
        baseline = self.snapshot()
        for stem, refused_key, refused_value in (
            ("statusline", "account.codex1", value),
            ("example", key, value),
            ("statusline", "model.", value),
            ("statusline", "model.sol", value),
            ("statusline", "model.unknown", value),
            ("statusline", "model.gpt-999-fixture", value),
            ("statusline", key, "blue"),
        ):
            with self.subTest(stem=stem, key=refused_key, value=refused_value):
                result = self.run_tune(stem, refused_key, refused_value, *approval)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(color_file.read_text(), original)
                self.assertEqual(self.snapshot(), baseline)
                self.assertFalse(repo.ssh_log.exists())
        result = self.run_tune("statusline", key, value, *approval)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = f"{original}\n{key}={value}\n"
        self.assertEqual(color_file.read_text(), expected)
        self.assertEqual(repo.git(repo.origin, "show", "main:config/statusline.conf"), expected.strip())
        self.assertIn(shlex.join(["statusline", key, value]), repo.ssh_log.read_text())
        self.assertEqual(self.waiting(), [])

    def test_an_override_added_after_save_does_not_block_model_color_publication(self) -> None:
        repo = self.repository
        config = repo.config.parent
        colors = "config/statusline.conf"
        models = config / "models.conf"
        _ = models.write_text("[codex.agents]\ngpt-6.2-sol=high\n")
        _ = (config / "machines.conf").write_text("aliases=mac\n")
        base = "account.claude1=brown\n"
        _ = (repo.checkout / colors).write_text(base)
        _ = repo.git(repo.checkout, "add", colors)
        _ = repo.git(repo.checkout, "commit", "-qm", "color fixture")
        _ = repo.git(repo.checkout, "push", "-q", "origin", "main")
        peer = repo.root / "peer"
        _ = repo.git(repo.root, "clone", "-q", "-b", "main", str(repo.origin), str(peer))
        key, value = "model.gpt-6.2-sol", "#304050"
        override = f"{key}.mac=#506070\n"
        with mock.patch.dict(os.environ, {**repo.env, "MODELS_CONFIG_FILE": str(models)}, clear=True):
            saved = live_settings.stage_save(live_settings.WaitingChange(
                "statusline", key, value, "tune(statusline): save model color"))
            assert isinstance(saved, live_settings.StagedSave)
            self.assertEqual(saved.replaced_value, "")
            _ = live_settings.write_mac("statusline", key, value, order=saved.order)
            _ = live_settings.stage_save(live_settings.WaitingChange("example", "amount", "3", "tune(example): save amount"))
            _ = (peer / colors).write_text(base + override)
            _ = repo.git(peer, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "-qam", "Add the Mac model color")
            _ = repo.git(peer, "push", "-q", "origin", "main")
            self.assertEqual(live_settings.publish(repo.checkout, wait=True), ["main: pushed", "mac: pulled"])

        expected = base + override + f"{key}={value}\n"
        self.assertEqual((repo.checkout / colors).read_text(), expected)
        self.assertEqual(repo.git(repo.origin, "show", f"main:{colors}"), expected.rstrip("\n"))
        self.assertIn("amount=3", repo.git(repo.origin, "show", "main:config/example.conf"))
        self.assertEqual(self.waiting(), [])

    def test_dotted_model_colors_can_be_saved_in_either_order(self) -> None:
        repo = self.repository
        config = repo.config.parent
        models = config / "models.conf"
        _ = models.write_text("[codex.agents]\ngpt-6.2-sol=high\ngpt-6.2-sol.preview=high\n")
        colors = config / "statusline.conf"
        keys = ("model.gpt-6.2-sol", "model.gpt-6.2-sol.preview")
        with mock.patch.dict(os.environ, {**repo.env, "CONF_DIR": str(config),
                                         "MODELS_CONFIG_FILE": str(models)}, clear=True):
            for order in (keys, keys[::-1]):
                with self.subTest(order=order):
                    _ = colors.write_text("")
                    for key in order:
                        self.assertEqual(live_settings.write_live("statusline", key, "#304050"), "")
                    for key in order:
                        self.assertEqual(live_settings.write_live("statusline", key, "#506070"), "#304050")
                    self.assertEqual(colors.read_text(), "".join(f"{key}=#506070\n" for key in order))

    def test_save_refuses_a_catalog_model_with_a_known_host_suffix(self) -> None:
        repo = self.repository
        config = repo.config.parent
        _ = (config / "machines.conf").write_text("aliases=natedev\n")
        _ = (config / "models.conf").write_text("[codex.agents]\ngpt-6.2-sol.natedev=high\n")
        colors = config / "statusline.conf"
        original = "model.gpt-6.2-sol=#304050\n"
        _ = colors.write_text(original)
        result = self.run_tune("statusline", "model.gpt-6.2-sol.natedev", "#506070",
                               "--why", "Chosen model color", "--user-words", "Save the color")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(".natedev is a host override", result.stderr)
        self.assertEqual(colors.read_text(), original)
        self.assertFalse(repo.ssh_log.exists())
        _ = colors.write_text(original + "model.gpt-6.2-sol.natedev=#506070\n")
        with mock.patch("socket.gethostname", return_value="natedev"):
            self.assertEqual(conf.effective_line(colors, "model.gpt-6.2-sol"), (1, "#506070"))

    def test_the_mac_fast_forward_keeps_new_model_colors_live(self) -> None:
        repo = self.repository
        relative = Path("config/statusline.conf")
        original = "account.claude1=brown\n"
        _ = (repo.checkout / relative).write_text(original)
        _ = repo.git(repo.checkout, "add", str(relative))
        _ = repo.git(repo.checkout, "commit", "-qm", "color fixture")
        _ = repo.git(repo.checkout, "push", "-q", "origin", "main")
        mac = repo.root / "mac"
        _ = repo.git(repo.root, "clone", "-q", "--branch", "main", str(repo.origin), str(mac))
        key, value = "model.gpt-6.2-sol", "#304050"
        pending_key, pending_value = "model.gpt-6.2-astra", "#506070"
        mac_catalog = mac / "config/models.conf"
        _ = mac_catalog.write_text("[codex.agents]\ngpt-6.2-sol=high\ngpt-6.2-astra=high\n")
        mac_env = {**repo.env, "CONF_DIR": str(mac_catalog.parent), "MODELS_CONFIG_FILE": str(mac_catalog),
                   "XDG_STATE_HOME": str(mac / "state")}
        with mock.patch.dict(os.environ, mac_env, clear=True):
            self.assertEqual(live_settings.main(["statusline", key, value]), 0)
            self.assertEqual(live_settings.main(["statusline", pending_key, pending_value]), 0)
        result = self.run_tune("statusline", key, value, "--why", "Chosen model color", "--user-words", "Save the color")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        _ = repo.git(mac, "fetch", "-q", "origin", "main")
        _ = mac_catalog.write_text("[codex.agents]\n")
        with mock.patch.dict(os.environ, mac_env, clear=True):
            self.assertFalse(waiting_rows.model_color_key("statusline", key))
            self.assertFalse(waiting_rows.model_color_key("statusline", pending_key))
            waiting_rows.move_to(mac, "origin/main")
            with pending_rows() as changes:
                self.assertEqual([(change.key, change.publish_on_main) for change in changes], [(pending_key, False)])
        self.assertEqual(repo.git(mac, "rev-parse", "HEAD"), repo.remote_tip())
        self.assertEqual((mac / relative).read_text(), f"{original}{key}={value}\n{pending_key}={pending_value}\n")

    def test_an_intermediate_pull_keeps_the_newer_live_save_and_its_receipt(self) -> None:
        repo = self.repository
        mac = repo.root / "mac"
        _ = repo.git(repo.root, "clone", "-q", "--branch", "main", str(repo.origin), str(mac))
        mac_env = {**repo.env, "CONF_DIR": str(mac / "config"), "XDG_STATE_HOME": str(mac / "state")}
        for value in ("3", "4"):
            result = self.run_tune("example", "amount", value, "--why", "Approved tuning", "--user-words", "yes")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        _ = repo.git(mac, "fetch", "-q", "origin", "main")
        sends = [shlex.split(line) for line in repo.ssh_log.read_text().splitlines() if "live_settings.py" in line]
        self.assertEqual(len(sends), 2)
        with mock.patch.dict(os.environ, mac_env, clear=True):
            for command in sends:
                environment = dict(word.split("=", 1) for word in command[:-5] if "=" in word)
                with mock.patch.dict(os.environ, environment):
                    self.assertEqual(live_settings.main(command[-3:]), 0)
            waiting_rows.move_to(mac, "origin/main^")
            self.assertIn("amount=4", (mac / "config/example.conf").read_text())
            with pending_rows() as changes:
                self.assertEqual([change.value for change in changes], ["4"])
            waiting_rows.move_to(mac, "origin/main")
            with pending_rows() as changes:
                self.assertEqual(changes, [])
        self.assertIn("amount=4", (mac / "config/example.conf").read_text())

    def test_a_return_to_the_committed_value_replaces_an_older_save_on_the_mac(self) -> None:
        repo = self.repository
        mac = repo.root / "mac"
        _ = repo.git(repo.root, "clone", "-q", "--branch", "main", str(repo.origin), str(mac))
        mac_env = {**repo.env, "CONF_DIR": str(mac / "config"), "XDG_STATE_HOME": str(mac / "state")}
        args = ("--why", "Approved tuning", "--user-words", "yes")
        original = repo.head()
        with mock.patch.dict(os.environ, repo.env, clear=True):
            saved = live_settings.stage_save(live_settings.WaitingChange(
                "example", "amount", "3", "tune(example): amount 2 -> 3"))
            assert isinstance(saved, live_settings.StagedSave)
            first = saved.replaced_value
        self.assertEqual(first, "2")
        self.assertEqual(repo.head(), original)
        second = self.run_tune("example", "amount", "2", *args)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(repo.git(repo.checkout, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"),
                         waiting_rows.ORDER_PATH)
        _ = repo.git(mac, "fetch", "-q", "origin", "main")
        with mock.patch.dict(os.environ, mac_env, clear=True):
            with mock.patch.dict(os.environ, {"LIVE_SETTINGS_SAVE_ORDER": "1"}):
                self.assertEqual(live_settings.main(["example", "amount", "3"]), 0)
            waiting_rows.move_to(mac, "origin/main", fast_forward=True)
            with pending_rows() as changes:
                self.assertEqual(changes, [])
            self.assertIn("amount=2", (mac / "config/example.conf").read_text())
            with mock.patch.dict(os.environ, {"LIVE_SETTINGS_SAVE_ORDER": "1"}):
                self.assertEqual(live_settings.main(["example", "amount", "3"]), 0)
            self.assertIn("amount=2", (mac / "config/example.conf").read_text())
            with pending_rows() as changes:
                self.assertEqual(changes, [])

    def test_tune_checks_the_value_after_the_locked_restore(self) -> None:
        repo = self.repository
        args = ["example", "amount", "2", "--why", "Approved tuning", "--user-words", "yes"]
        with mock.patch.dict(os.environ, repo.env, clear=True), redirect_stdout(io.StringIO()) as output:
            with waiting_rows.locked_journal(waiting_rows.journal_path()) as pending:
                self.assertTrue(pending.accept("example", "amount", waiting_rows.AcceptedSave(1, "3")))
                pending.save()
            self.assertIn("amount=2", repo.config.read_text())
            self.assertEqual(tune.main(args), 0, output.getvalue())
        self.assertIn("live now: amount=2", output.getvalue())
        self.assertIn("amount=2", repo.config.read_text())
        self.assertEqual(repo.git(repo.checkout, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"),
                         waiting_rows.ORDER_PATH)

    def test_a_save_after_the_clock_steps_back_has_a_greater_order(self) -> None:
        repo = self.repository
        args = ("--why", "Approved tuning", "--user-words", "yes")
        with mock.patch.dict(os.environ, repo.env, clear=True), redirect_stdout(io.StringIO()) as output:
            for clock, value in ((400, "3"), (200, "4")):
                with mock.patch.object(time, "time_ns", return_value=clock):
                    self.assertEqual(tune.main(["example", "amount", value, *args]), 0, output.getvalue())
                orders = waiting_rows.published_orders(repo.checkout, "HEAD")
                self.assertEqual(orders["example"]["amount"], int(value) - 2)
                self.assertEqual(self.waiting(), [])

    def test_already_set_value_prints_one_line_without_a_write_and_completes_the_mac_pull(self) -> None:
        before = self.snapshot()
        result = self.run_tune("example", "amount", "2", "--why", "Already approved", "--user-words", "yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "already amount=2\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.snapshot(), before)
        self.assertIn("git fetch -q --no-write-fetch-head", self.repository.ssh_log.read_text())

    def waiting(self) -> list[live_settings.WaitingChange]:
        with mock.patch.dict(os.environ, self.repository.env, clear=True), pending_rows() as journal:
            return list(journal)

    def test_the_same_save_can_publish_after_the_journal_write_fails(self) -> None:
        repo = self.repository
        args = ["example", "amount", "3", "--why", "Approved tuning", "--user-words", "yes"]
        errors = io.StringIO()
        with mock.patch.dict(os.environ, repo.env, clear=True), redirect_stderr(errors), \
                mock.patch.object(waiting_rows, "write_text_atomic", side_effect=OSError("journal write failed")):
            self.assertEqual(tune.main(args), 2)
        self.assertIn("journal write failed", errors.getvalue())
        self.assertEqual(self.waiting(), [])
        self.assertIn("amount=2", repo.config.read_text())

        retry = self.run_tune(*args)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertIn("amount=3", repo.git(repo.origin, "show", "main:config/example.conf"))
        self.assertEqual(self.waiting(), [])

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
                _ = live_settings.stage_save(live_settings.WaitingChange(
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
        for value in ("#af875f", "part#fragment"):
            with self.subTest(value=value), mock.patch.dict(os.environ, repo.env, clear=True), \
                    mock.patch("platform.system", return_value="Darwin"), \
                    mock.patch.object(tune, "_forward", return_value=0) as forward:
                arguments = ["statusline", "account.claude1", value, "--why", "Chosen color", "--user-words", "Save"]
                self.assertEqual(tune.main(arguments), 0)
                forward.assert_called_once_with(arguments, repo.env.get("ZONE_SSH") or "ssh")


if __name__ == "__main__":
    _ = unittest.main()

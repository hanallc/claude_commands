"""Check complete configuration fixtures and successive value changes."""

import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.lib import conf
from scripts.lib.registry_rows import registry_path
from scripts.lint.memory_test_support import (
    ConfigFixtureRefused, commit_config_fixture, config_environment, shifted_number, write_config_fixture,
)
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class ConfigFixtureTests(unittest.TestCase):
    def test_config_environment_clears_an_inherited_registry_pin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            caller_registry = "/home/worker/.claude/config/models.conf"
            with mock.patch.dict(os.environ, {"MODELS_CONFIG_FILE": caller_registry}):
                fixture_environment = {**os.environ, **config_environment(folder)}
                self.assertFalse(fixture_environment.get("MODELS_CONFIG_FILE"))
                self.assertEqual(registry_path(fixture_environment), folder / "models.conf")
                self.assertEqual(os.environ["MODELS_CONFIG_FILE"], caller_registry)

    def test_partial_config_keeps_values_and_receives_committed_defaults_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.conf"
            _ = template.write_text(
                "# whole: 1..\nmax_fix_attempts=3\n"
                + "# whole: 1..\nmax_reopens=3\n"
                + "# whole: 1..4\ncount=4\n"
                + "# whole: 2..2\nfixed=2\n"
                + "# range: 0..10\ntimeout=9\n"
                + "# range: 0..0.5\nretry=0\n"
                + "# one of: 1, 2\nmode=1\n"
                + "unbounded=7\n")
            path = root / "direct.conf"
            _ = path.write_text("max_fix_attempts=17\nfixture_only=kept\n")
            with mock.patch("scripts.lint.memory_test_support._template", return_value=template):
                _ = write_config_fixture(path, "max_reopens=19\n")
                _ = write_config_fixture(path)
            text = path.read_text()
        expected = {
            "max_fix_attempts": "17", "max_reopens": "19", "fixture_only": "kept",
            "count": "3", "fixed": "2", "timeout": "4.5", "retry": "0.25", "mode": "1", "unbounded": "7",
        }
        actual = dict(line.split("=", 1) for line in text.splitlines()
                      if "=" in line and not line.startswith("#"))
        self.assertEqual(actual, expected)
        self.assertIn("# whole: 1..\nmax_fix_attempts=17\n", text)
        self.assertIn("# test config shift: 4 3\n# whole: 1..4\ncount=3\n", text)

    def test_config_environment_shifts_unpinned_values_and_uses_short_deadlines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            committed = root / "committed"
            committed.mkdir()
            _ = (committed / "copies.conf").write_text(
                "# range: 1..\ngit_timeout_seconds=600\n# whole: 1..\nfixture_count=4\n")
            _ = (committed / "build_admission.conf").write_text("# range: 0..\nmin_pass_gap_seconds=0.25\n")
            private = root / "private"
            def template(name: str) -> Path:
                return committed / name

            with mock.patch("scripts.lint.memory_test_support._template", side_effect=template):
                _ = config_environment(private)
            settings = conf.read(private / "copies.conf")
            self.assertEqual(settings.whole("fixture_count"), 5)
            self.assertEqual(settings.number("git_timeout_seconds"), 30)
            self.assertEqual(conf.read(private / "build_admission.conf").number("min_pass_gap_seconds"), 0)
            self.assertIn("# test config shift: 4 5", (private / "copies.conf").read_text())
            self.assertNotIn("# test config shift: 600", (private / "copies.conf").read_text())

    def test_decimal_shift_uses_high_midpoint_when_only_high_side_has_room(self) -> None:
        setting = conf.Setting("4", "# range: 4..4.5", 0)
        self.assertEqual(shifted_number(setting), "4.25")

    def test_narrow_decimal_range_uses_an_endpoint_or_an_adjacent_float(self) -> None:
        narrow = conf.Setting("1", "# range: 1..1.0000000000000002", 0)
        smallest = conf.Setting("5e-324", "# range: 5e-324..", 0)
        self.assertEqual(shifted_number(narrow), "1.0000000000000002")
        self.assertEqual(float(shifted_number(smallest)), math.nextafter(float(smallest.value), math.inf))

    def test_successive_overrides_keep_prior_values_and_allow_an_explicit_empty_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lint.conf"
            _ = write_config_fixture(path, "sweep_ci_targets=/private/ci\nfixture_only=first\n")
            _ = write_config_fixture(path, "sweep_floor_roots=/private/root\nfixture_only=\n")
            text = path.read_text()
        self.assertIn("sweep_ci_targets=/private/ci\n", text)
        self.assertIn("sweep_floor_roots=/private/root\n", text)
        self.assertIn("fixture_only=\n", text)
        self.assertNotIn("fixture_only=first\n", text)

    def test_named_template_completes_an_existing_alternate_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-mac.conf"
            _ = path.write_text("host=private-host\n")
            _ = write_config_fixture(path, "offload=on\n", template="mac_test")
            text = path.read_text()
        self.assertIn("host=private-host\n", text)
        self.assertIn("offload=on\n", text)
        self.assertIn("leftover_timeout_s=", text)

    def test_existing_and_explicit_limits_follow_their_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aws_target.conf"
            _ = path.write_text("# range: 0..\nfixed_usd_per_month=0\n")
            _ = write_config_fixture(path, "kinds=test,doc\n# whole: 0..\nslots=0\n")
            _ = write_config_fixture(path, "offload=on\n")
            text = path.read_text()
        self.assertIn("# range: 0..\nfixed_usd_per_month=0\n", text)
        self.assertIn("# whole: 0..\nslots=0\n", text)
        self.assertIn("kinds=test,doc\n", text)


if __name__ == "__main__":
    _ = unittest.main()


def git(folder: Path, *arguments: str, environment: dict[str, str] | None = None) -> str:
    return subprocess.run(["git", "-C", str(folder), *arguments], check=True, capture_output=True, text=True,
                          env=environment if environment is not None else isolated_git_environment(),
                          timeout=TEST_HANG_GUARD_SECS).stdout.strip()


def isolated_git_environment() -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    return environment | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def other_repository(root: Path) -> Path:
    repository = root / "other"
    repository.mkdir()
    _ = git(repository, "init", "-q")
    _ = git(repository, "-c", "user.name=Other", "-c", "user.email=other@example.invalid",
            "commit", "-q", "--allow-empty", "-m", "other start")
    return repository


class CommitConfigFixtureTests(unittest.TestCase):
    def test_a_caller_git_dir_never_receives_the_fixture_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = other_repository(root)
            head, config = git(other, "rev-parse", "HEAD"), (other / ".git/config").read_text()
            folder = root / "conf"
            folder.mkdir()
            path = folder / "build_admission.conf"
            _ = path.write_text("value=1\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                                              "GIT_INDEX_FILE": str(other / ".git/index")}):
                commit_config_fixture(path)
            self.assertEqual(git(other, "rev-parse", "HEAD"), head)
            self.assertEqual((other / ".git/config").read_text(), config)
            self.assertEqual(git(folder, "log", "--format=%an %s"), "Config fixture known good configuration")
            self.assertEqual(git(folder, "ls-files"), "build_admission.conf")

    def test_a_folder_inside_a_repository_or_outside_the_temporary_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = other_repository(root)
            head = git(other, "rev-parse", "HEAD")
            inside = other / "conf"
            inside.mkdir()
            path = inside / "build_admission.conf"
            _ = path.write_text("value=1\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigFixtureRefused, "inside the git repository"):
                commit_config_fixture(path)
            self.assertEqual(git(other, "rev-parse", "HEAD"), head)
            self.assertEqual(git(other, "status", "--porcelain"), "?? conf/")
            outside = root / "outside"
            outside.mkdir()
            with mock.patch.object(tempfile, "gettempdir", return_value=str(root / "private")):
                with self.assertRaisesRegex(ConfigFixtureRefused, "not a private folder"):
                    commit_config_fixture(outside / "build_admission.conf")
            self.assertFalse((outside / ".git").exists())
            with self.assertRaisesRegex(ConfigFixtureRefused, "not a private folder"):
                commit_config_fixture(Path(tempfile.gettempdir()) / "build_admission.conf")

    def test_a_second_commit_in_the_same_folder_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "build_admission.conf"
            _ = path.write_text("value=1\n", encoding="utf-8")
            commit_config_fixture(path)
            _ = path.write_text("value=2\n", encoding="utf-8")
            commit_config_fixture(path)
            self.assertEqual(len(git(Path(directory), "log", "--format=%s").splitlines()), 2)


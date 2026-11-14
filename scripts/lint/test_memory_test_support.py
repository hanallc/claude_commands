"""Check complete configuration fixtures and successive value changes."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.lint.memory_test_support import ConfigFixtureRefused, commit_config_fixture, write_config_fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class ConfigFixtureTests(unittest.TestCase):
    def test_partial_config_keeps_values_and_receives_committed_defaults_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "direct.conf"
            _ = path.write_text("MAX_FIX_ATTEMPTS=17\nfixture_only=kept\n")
            _ = write_config_fixture(path, "MAX_REOPENS=19\n")
            text = path.read_text()
        committed = Path(__file__).resolve().parents[2] / "config" / "direct.conf"
        expected = dict(line.split("=", 1) for line in committed.read_text().splitlines()
                        if "=" in line and not line.startswith("#"))
        expected.update(MAX_FIX_ATTEMPTS="17", MAX_REOPENS="19", fixture_only="kept")
        actual = dict(line.split("=", 1) for line in text.splitlines()
                      if "=" in line and not line.startswith("#"))
        self.assertEqual(actual, expected)
        self.assertIn("# whole: 1..\nMAX_FIX_ATTEMPTS=17\n", text)

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


"""Check the lifetime, directory, and cwd of cached Git identities."""
from __future__ import annotations

import unittest
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
import tempfile
from unittest import mock
from typing import override

from scripts.build_queue import step_estimator
from scripts.build_queue.admission_records import NEXTEST_BUILD_STEP, NEXTEST_TEST_STEP, RepositoryIdentity
from scripts.buildlog import record, repository
from scripts.buildlog import store
from scripts.lib import conf
from scripts.lint.memory_test_support import write_config_fixture


class GitIdentityCacheTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        step_estimator.clear_git_identity_cache()
        self.addCleanup(step_estimator.clear_git_identity_cache)

    def test_matching_directory_and_cwd_reuse_identity_but_either_change_reads_git(self) -> None:
        with mock.patch.object(record, "git_directory", side_effect=["/first", "/first/../first", "/second", "/first"]), \
                mock.patch.object(record, "git_facts", return_value={"worktree": None, "repo_path": None}) as facts, \
                mock.patch.object(repository, "repository_key", side_effect=["git-root:first", "git-root:second", "git-root:first"]) as key:
            first = step_estimator.git_identity(["cargo", "build"], "/caller")
            repeated = step_estimator.git_identity(["cargo", "test"], "/caller")
            second = step_estimator.git_identity(["cargo", "build", "--manifest-path", "/second/Cargo.toml"], "/caller")
            other_cwd = step_estimator.git_identity(["cargo", "build", "--manifest-path", "/first/Cargo.toml"], "/other")
        self.assertEqual(first, (RepositoryIdentity("git-root:first", "caller"), "caller"))
        self.assertEqual(repeated, first)
        self.assertEqual(second, (RepositoryIdentity("git-root:second", "caller"), "caller"))
        self.assertEqual(other_cwd, (RepositoryIdentity("git-root:first", "other"), "other"))
        self.assertEqual(facts.call_args_list, [mock.call("/first"), mock.call("/second"), mock.call("/first")])
        self.assertEqual(key.call_args_list, facts.call_args_list)

    def test_clearing_after_origin_or_root_changes_reads_the_new_identity(self) -> None:
        for before, after in (("https://example.org/old", "https://example.org/new"),
                              ("git-root:old", "git-root:new")):
            with self.subTest(before=before):
                step_estimator.clear_git_identity_cache()
                with mock.patch.object(record, "git_facts", return_value={"worktree": "/repo", "repo_path": "/repo"}), \
                        mock.patch.object(repository, "repository_key", side_effect=[before, after]) as key:
                    old = step_estimator.git_identity(["cargo", "build"], "/repo")
                    self.assertEqual(step_estimator.git_identity(["cargo", "test"], "/repo"), old)
                    step_estimator.clear_git_identity_cache()
                    new = step_estimator.git_identity(["cargo", "build"], "/repo")
                self.assertNotEqual(old, new)
                identity = new[0]
                self.assertIsInstance(identity, RepositoryIdentity)
                assert isinstance(identity, RepositoryIdentity)
                self.assertEqual(identity.key, after)
                self.assertEqual(key.call_count, 2)


class ManifestIdentityTests(unittest.TestCase):
    base: Path = Path()

    @override
    def setUp(self) -> None:
        step_estimator.clear_git_identity_cache()
        self.addCleanup(step_estimator.clear_git_identity_cache)
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.base = Path(temporary).resolve()
        self.enterContext(mock.patch.dict(os.environ, {
            "GIT_CEILING_DIRECTORIES": str(self.base.parent), "MANIFEST_PATH": "",
        }))
        is_file = Path.is_file
        def fixture_file(path: Path) -> bool:
            if path.name == "Cargo.toml" and not path.is_relative_to(self.base):
                return False
            return is_file(path)
        _ = self.enterContext(mock.patch.object(Path, "is_file", fixture_file))

    def package(self, directory: Path) -> Path:
        directory.mkdir(parents=True)
        source = directory / "src"
        source.mkdir()
        _ = (source / "lib.rs").write_text("")
        manifest = directory / "Cargo.toml"
        _ = manifest.write_text('[package]\nname = "sample"\nversion = "0.1.0"\n')
        return manifest.resolve()

    def test_gitless_steps_use_the_nearest_real_manifest(self) -> None:
        single = self.package(self.base / "single")
        selected = self.package(self.base / "selected")
        alias = self.base / "alias"
        alias.symlink_to(single.parent, target_is_directory=True)
        cases: tuple[tuple[Path, list[str], Path], ...] = (
            (single.parent / "src", [], single),
            (alias, [], single),
            (single.parent, ["--manifest-path", str(selected)], selected),
            (single.parent, ["--manifest-path=../selected/Cargo.toml"], selected),
        )
        for cwd, options, manifest in cases:
            with self.subTest(cwd=cwd, options=options):
                step_estimator.clear_git_identity_cache()
                identity, _ = step_estimator.git_identity(["cargo", "build", *options], str(cwd))
                self.assertEqual(identity, RepositoryIdentity("manifest-path:" + str(manifest), manifest.parent.name))
                self.assertEqual(record.repository_key(record.git_directory(["cargo", "build", *options], str(cwd))),
                                 "manifest-path:" + str(manifest))

    def test_gitless_identity_needs_no_cargo_or_subprocess(self) -> None:
        manifest = self.package(self.base / "single")
        empty_path = self.base / "empty-path"
        empty_path.mkdir()
        for ceiling in (self.base.parent, manifest.parent):
            with self.subTest(ceiling=ceiling), \
                    mock.patch.dict(os.environ, {"PATH": str(empty_path), "GIT_CEILING_DIRECTORIES": str(ceiling)}), \
                    mock.patch.object(subprocess, "run", side_effect=AssertionError("identity started a command")):
                self.assertEqual(repository.repository_key(str(manifest.parent / "src")), "manifest-path:" + str(manifest))

    def test_gitless_folder_without_a_manifest_keeps_unknown_identity(self) -> None:
        empty = self.base / "empty"
        empty.mkdir()
        self.assertEqual(repository.repository_key(str(empty)), store.UNKNOWN_IDENTITY)

    def test_git_keys_remain_separate_from_manifest_keys(self) -> None:
        manifest = self.package(self.base / "gitless")
        git = self.package(self.base / "git").parent
        environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": "Test", "GIT_COMMITTER_NAME": "Test",
                       "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        def command(*arguments: str) -> str:
            return subprocess.run(["git", "-C", str(git), *arguments], env=environment,
                                  capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        _ = command("init", "-q", "--template=")
        self.assertEqual(repository.repository_key(str(git)), store.UNKNOWN_IDENTITY)
        _ = command("-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null",
                    "commit", "--allow-empty", "-qm", "first")
        root = command("rev-parse", "HEAD")
        self.assertEqual(repository.repository_key(str(git)), "git-root:" + root)
        _ = command("remote", "add", "origin", "git@example.org:owner/project.git")
        self.assertEqual((repository.repository_key(str(git)), repository.repository_key(str(manifest.parent))),
                         ("https://example.org/owner/project", "manifest-path:" + str(manifest)))

if __name__ == "__main__":
    _ = unittest.main()


class NextestStepPeakTests(unittest.TestCase):
    def test_config_refuses_a_nextest_test_percentile_below_p90(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration = Path(directory) / "build_admission.conf"
            _ = write_config_fixture(configuration)
            key = "nextest_test_peak_percentile"
            conf.check(configuration, key, "90")
            with self.assertRaisesRegex(conf.Unusable, "outside range"):
                conf.check(configuration, key, "89")

    def test_build_and_test_read_their_own_peak_in_the_same_repository(self) -> None:
        instant = datetime(2026, 11, 17, tzinfo=UTC)
        repo = RepositoryIdentity("origin:fixture", "fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "admission/anon_peaks.jsonl"
            configuration = root / "build_admission.conf"
            _ = write_config_fixture(configuration)

            def sample(step: str, peak: int, *, project: str = repo.key, promise: int = 1000) -> None:
                row = {"host": "fixture", "repo_identity": project, "step": step,
                       "ended_at": (instant - timedelta(minutes=1)).isoformat(),
                       "anon_peak_bytes": 1000, "need": promise}
                field = "build_phase_peak_bytes" if step == NEXTEST_BUILD_STEP else "test_phase_peak_bytes"
                store.append_line(history, {**row, field: peak})

            def estimate(step: str) -> int:
                with mock.patch.dict(os.environ, {"CONF_DIR": str(root)}):
                    return step_estimator.expected_peak(repo, step, "fixture", instant, root,
                                                        first_peak_bytes=3000, share_percent=50).bytes

            self.assertEqual(estimate(NEXTEST_TEST_STEP), 3000)
            sample(NEXTEST_BUILD_STEP, 800)
            sample(NEXTEST_BUILD_STEP, 900, project="origin:other")
            for peak in range(10, 101, 10):
                sample("nextest", peak)
            sample(NEXTEST_TEST_STEP, 1000, project="origin:other")
            self.assertEqual(estimate(NEXTEST_BUILD_STEP), 800)
            self.assertEqual(estimate(NEXTEST_TEST_STEP), 90)
            default_config = configuration.read_text()
            _ = write_config_fixture(configuration, "nextest_test_peak_percentile=100")
            self.assertEqual(estimate(NEXTEST_TEST_STEP), 100)
            _ = configuration.write_text(default_config)

            sample(NEXTEST_TEST_STEP, 300, promise=90)
            for _ in range(20):
                sample(NEXTEST_TEST_STEP, 90)
            self.assertEqual(estimate(NEXTEST_TEST_STEP), 300)

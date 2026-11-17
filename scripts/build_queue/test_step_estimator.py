"""Check the lifetime, directory, and cwd of cached Git identities."""
from __future__ import annotations

import unittest
import os
from pathlib import Path
import subprocess
import tempfile
from unittest import mock
from typing import override

from scripts.build_queue import step_estimator
from scripts.build_queue.admission_records import RepositoryIdentity
from scripts.buildlog import record, repository
from scripts.buildlog import store


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
    def package(self, directory: Path, *, workspace: str = "") -> Path:
        directory.mkdir(parents=True)
        source = directory / "src"
        source.mkdir()
        _ = (source / "lib.rs").write_text("")
        manifest = directory / "Cargo.toml"
        _ = manifest.write_text('[package]\nname = "sample"\nversion = "0.1.0"\n' + workspace)
        return manifest.resolve()

    def test_gitless_steps_use_the_real_manifest_selected_by_cargo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            single = self.package(base / "single")
            workspace = base / "workspace"
            workspace.mkdir()
            _ = (workspace / "Cargo.toml").write_text('[workspace]\nmembers = ["member"]\nexclude = ["excluded"]\n')
            member = self.package(workspace / "member")
            excluded = self.package(workspace / "excluded")
            detached = self.package(base / "detached", workspace='workspace = "../workspace"\n')
            _ = (workspace / "Cargo.toml").write_text(
                '[workspace]\nmembers = ["member", "../detached"]\nexclude = ["excluded"]\n')
            alias = base / "alias"
            alias.symlink_to(single.parent, target_is_directory=True)
            cases: tuple[tuple[Path, list[str], Path], ...] = ((single.parent / "src", [], single),
                     (alias, [], single),
                     (base, ["--manifest-path", str(member)], workspace / "Cargo.toml"),
                     (base, ["--manifest-path=" + str(detached)], workspace / "Cargo.toml"),
                     (excluded.parent, [], excluded))
            for cwd, options, manifest in cases:
                with self.subTest(cwd=cwd, options=options):
                    step_estimator.clear_git_identity_cache()
                    identity, _ = step_estimator.git_identity(["cargo", "build", *options], str(cwd))
                    self.assertEqual(identity, RepositoryIdentity("manifest-path:" + str(manifest), manifest.parent.name))
                    self.assertEqual(record.repository_key(record.git_directory(["cargo", "build", *options], str(cwd))),
                                     "manifest-path:" + str(manifest))

    def test_git_keys_remain_separate_from_manifest_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            manifest = self.package(base / "gitless")
            git = self.package(base / "git").parent
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

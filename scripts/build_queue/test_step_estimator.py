"""Check the lifetime, directory, and cwd of cached Git identities."""
from __future__ import annotations

import unittest
from unittest import mock
from typing import override

from scripts.build_queue import step_estimator
from scripts.build_queue.admission_records import RepositoryIdentity
from scripts.buildlog import record, repository


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


if __name__ == "__main__":
    _ = unittest.main()

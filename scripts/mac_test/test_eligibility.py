"""Mac offload eligibility tests."""

import unittest

from scripts.mac_test.eligibility import KnownRepository


class RepositoryPolicyNameTests(unittest.TestCase):
    def test_git_dir_key_uses_the_readable_name(self) -> None:
        repository = KnownRepository("hana", "git-dir:/source/hana/.git")
        self.assertEqual(repository.policy_name, "hana")

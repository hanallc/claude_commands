"""A merge red that is red on the base too goes to the merge director, not to the unit."""
from __future__ import annotations

import json
import unittest
from typing import final

from scripts.production import checkpoint_gate
from scripts.production.merge_checkpoint_test_support import MergeCheckpointFixture, configure_test_environment

MERGE_TEST = "- **Merge tests:** `verify.sh test cargo-tile`"
GATE = ["py", "/repo/scripts/production/merge_gate.py", "--checkout", ".", "--tests-only"]


def setUpModule() -> None:
    configure_test_environment()


@final
class RedNameTests(unittest.TestCase):
    def test_gate_names_leave_out_modules_green_on_rerun_alone(self) -> None:
        output = ("--- accounts/test_accounts.py exit=1 ---\n--- direct/test_claim.py exit=1 ---\n"
                  + "accounts: RED 391.25s\n--- accounts/test_accounts.py first run exit=1 ---\n"
                  + "--- accounts/test_accounts.py rerun alone exit=1 ---\n"
                  + "--- direct/test_claim.py passed on rerun alone; first run exit=1 ---\n")
        self.assertEqual(checkpoint_gate.red_names(GATE, output), frozenset({"accounts/test_accounts.py"}))

    def test_each_command_kind_reads_only_its_own_names(self) -> None:
        output = "        FAIL [   1.000s] (1/2) fixture::tests unit_red\n--- lib/test_first.py exit=1 ---\n"
        self.assertEqual(checkpoint_gate.red_names(["verify.sh", "test", "alpha"], output), frozenset({"unit_red"}))
        self.assertEqual(checkpoint_gate.red_names(GATE, output), frozenset({"lib/test_first.py"}))

    def test_a_gate_command_narrows_with_module_options(self) -> None:
        self.assertEqual(checkpoint_gate.only_tests(GATE, frozenset({"lib/test_b.py", "hooks/test_a.py"})),
                         [*GATE, "--module", "hooks/test_a.py", "--module", "lib/test_b.py"])


@final
class BaseRedTests(MergeCheckpointFixture):
    def red_files(self, rows: dict[str, str]) -> None:
        """Make each named test fail while its file is in the tested tree."""
        _ = (self.state / "tree-fail-rows").write_text(json.dumps(rows), encoding="utf-8")
        self.production_doc(merge_tests_line=MERGE_TEST)

    def merge_log(self, sha: str) -> str:
        return (self.root / "scratch" / f"merge_{sha[:7]}_test.log").read_text(encoding="utf-8")

    def test_a_red_already_on_main_goes_to_the_merge_director_and_holds_the_unit(self) -> None:
        self.red_files({"src/main-red.txt": "main_red"})
        _ = self.push_main_change("src/main-red.txt", "red on main\n")
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        output = result.process.stdout
        self.assertEqual(result.process.returncode, 1, output + result.process.stderr)
        self.assertEqual(output.count("merge director: merge test 1 (main_red) red on base "), 1, output)
        self.assertIn("red: held", output)
        self.assertIn("send alpha: your checkpoint adds no red", output)
        self.assertNotIn("send alpha: repair", output)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), result.before)
        self.assertEqual(self.events("verify.sh"),
                         [["test", "cargo-tile"], ["test", "cargo-tile"], ["test", "cargo-tile", "--filter", "main_red"]])
        self.assertIn("merge_test_1_BASE_EXIT=1", self.merge_log(sha))

    def test_a_mixed_red_sends_the_unit_only_its_own_names(self) -> None:
        self.red_files({"src/main-red.txt": "main_red", "src/unit-red.txt": "unit_red"})
        _ = self.push_main_change("src/main-red.txt", "red on main\n")
        sha = self.checkpoint("src/unit-red.txt", "red in the unit\n")
        result = self.run_checkpoint(sha)
        output = result.process.stdout
        self.assertEqual(result.process.returncode, 1, output + result.process.stderr)
        self.assertIn("merge director: merge test 1 (main_red) red on base ", output)
        self.assertIn("red: failed", output)
        self.assertIn("send alpha: repair merge test 1 (unit_red) and send a new checkpoint", output)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.events("verify.sh")[-1],
                         ["test", "cargo-tile", "--filter", "main_red", "--filter", "unit_red"])

    def test_the_units_own_red_reaches_only_the_unit(self) -> None:
        self.red_files({"src/unit-red.txt": "unit_red"})
        sha = self.checkpoint("src/unit-red.txt", "red in the unit\n")
        result = self.run_checkpoint(sha)
        output = result.process.stdout
        self.assertEqual(result.process.returncode, 1, output + result.process.stderr)
        self.assertNotIn("merge director:", output)
        self.assertIn("send alpha: repair merge test 1 (unit_red) and send a new checkpoint", output)
        self.assertIn("merge_test_1_BASE_EXIT=0", self.merge_log(sha))


if __name__ == "__main__":
    _ = unittest.main()

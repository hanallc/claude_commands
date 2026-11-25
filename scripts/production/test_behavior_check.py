"""Check test changes against fixture repositories."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import cast, override
from unittest import mock

from scripts.lib.rerun_test_support import ScriptRepositoryTestCase
from scripts.production import behavior_check


class BehaviorCheckTests(ScriptRepositoryTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.environment["SHOWRUNNER_STATE_DIR"] = str(self.root / "showrunner")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment, clear=True)))
        self.seed()

    def commit_files(self) -> str:
        _ = self.git("add", "-A")
        _ = self.git("commit", "--quiet", "-m", "fixture")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def test_non_test_changes_use_only_the_name_diff(self) -> None:
        base = self.git("rev-parse", "HEAD").stdout.strip()
        _ = self.write(self.repo / "application.py", "invalid Python that must not be parsed\n")
        head = self.commit_files()
        with mock.patch.object(behavior_check, "test_lines", wraps=behavior_check.test_lines) as parse:
            with mock.patch.object(behavior_check, "test_blobs", wraps=behavior_check.test_blobs) as read:
                self.assertEqual(behavior_check.changed_tests(behavior_check.GitRange(self.repo, base, head)).tests, ())
        parse.assert_not_called()
        read.assert_not_called()

    def test_reports_altered_and_removed_tests_but_skips_new_and_moved_tests(self) -> None:
        original = ("class Demo:\n"
                    + "    @fixture\n    def test_alter(self):\n        self.assertTrue(True)\n\n"
                    + "    async def test_remove(self):\n        pass\n")
        altered = ("class Demo:\n"
                   + "    @fixture\n    def test_alter(self):\n        self.assertTrue(False)\n\n"
                   + "    def test_new(self):\n        pass\n")
        moved = "def test_move():\n    assert 3\n"
        _ = self.write(self.repo / "test_fixture.py", original)
        _ = self.write(self.repo / "test_before.py", moved)
        base = self.commit_files()
        _ = (self.repo / "test_before.py").rename(self.repo / "test_after.py")
        _ = self.write(self.repo / "test_fixture.py", altered)
        head = self.commit_files()
        result = behavior_check.changed_tests(behavior_check.GitRange(self.repo, base, head))
        self.assertEqual(result.tests, (
            behavior_check.TestChange("test_fixture.py::Demo.test_alter",
                                      "    @fixture\n    def test_alter(self):\n        self.assertTrue(True)\n",
                                      "    @fixture\n    def test_alter(self):\n        self.assertTrue(False)\n"),
            behavior_check.TestChange("test_fixture.py::Demo.test_remove",
                                      "    async def test_remove(self):\n        pass\n", ""),
        ))

    def test_rust_checks_plain_and_qualified_attributes_and_skips_comments(self) -> None:
        original = """mod tests {
    /* #[test] fn commented() {} */
    #[test]
    fn plain() {
        let braces = r#"}"#;
        assert!(true);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn qualified() {
        assert!(true);
    }
}
"""
        _ = self.write(self.repo / "src/lib.rs", original)
        base = self.commit_files()
        _ = self.write(self.repo / "src/lib.rs", original.replace("assert!(true)", "assert!(false)").replace(
            "fn commented()", "fn ignored()"))
        head = self.commit_files()
        result = behavior_check.changed_tests(behavior_check.GitRange(self.repo, base, head))
        self.assertEqual([test.test for test in result.tests], ["src/lib.rs::tests::plain", "src/lib.rs::tests::qualified"])
        self.assertIn('let braces = r#"}"#;', result.tests[0].new)
        self.assertIn('assert!(false)', result.tests[0].new)

    def test_merge_commit_reuses_the_record_for_the_same_test_changes(self) -> None:
        test = self.repo / "test_example.py"
        _ = self.write(test, "def test_example():\n    assert 1\n")
        base = self.commit_files()
        _ = self.git("switch", "-c", "topic")
        _ = self.write(test, "def test_example():\n    assert 2\n")
        topic = self.commit_files()
        changes = behavior_check.changed_tests(behavior_check.GitRange(self.repo, base, topic))
        behavior_check.write_record(changes.key, behavior_check.Clear("No user decision found."))
        _ = self.git("switch", "main")
        _ = self.write(self.repo / "application.txt", "unrelated merge base change\n")
        main = self.commit_files()
        _ = self.git("merge", "--no-ff", "--no-edit", "topic")
        merged = self.git("rev-parse", "HEAD").stdout.strip()
        merge_range = behavior_check.GitRange(self.repo, main, merged)
        self.assertEqual(behavior_check.changed_tests(merge_range).key, changes.key)
        behavior_check.require_review(merge_range)

    def test_record_round_trip_keeps_a_negative_answer_and_its_time(self) -> None:
        key = "f" * 64
        answer = behavior_check.Answered("Q2", behavior_check.Approval.NO, "No", datetime.now(timezone.utc))
        behavior_check.write_record(key, answer)
        self.assertEqual(behavior_check.read_record(key), answer)

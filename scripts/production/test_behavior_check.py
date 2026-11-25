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

    def changes(self, base: str, head: str) -> behavior_check.ChangedTests:
        changes = behavior_check.changed_tests(behavior_check.GitRange(self.repo, base, head))
        assert isinstance(changes, behavior_check.ChangedTests)
        return changes

    def test_non_test_changes_use_only_the_tree_diff(self) -> None:
        base = self.git("rev-parse", "HEAD").stdout.strip()
        _ = self.write(self.repo / "application.py", "invalid Python that must not be parsed\n")
        head = self.commit_files()
        with mock.patch.object(behavior_check, "test_lines", wraps=behavior_check.test_lines) as parse:
            with mock.patch.object(behavior_check, "test_blobs", wraps=behavior_check.test_blobs) as read:
                self.assertIs(behavior_check.changed_tests(behavior_check.GitRange(self.repo, base, head)),
                              behavior_check.NoChangedTests.EMPTY)
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
        result = self.changes(base, head)
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
        result = self.changes(base, head)
        self.assertEqual([test.test for test in result.tests], ["src/lib.rs::tests::plain", "src/lib.rs::tests::qualified"])
        self.assertIn('let braces = r#"}"#;', result.tests[0].new)
        self.assertIn('assert!(false)', result.tests[0].new)

    def test_merge_commit_keeps_the_key_for_the_same_patch(self) -> None:
        test = self.repo / "test_example.py"
        _ = self.write(test, "def test_example():\n    assert 1\n")
        base = self.commit_files()
        _ = self.git("switch", "-c", "topic")
        _ = self.write(test, "def test_example():\n    assert 2\n")
        topic = self.commit_files()
        changes = self.changes(base, topic)
        _ = self.git("switch", "main")
        _ = self.write(self.repo / "application.txt", "unrelated merge base change\n")
        main = self.commit_files()
        _ = self.git("merge", "--no-ff", "--no-edit", "topic")
        merged = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(self.changes(main, merged).key, changes.key)

    def test_record_round_trip_keeps_a_negative_answer_and_its_time(self) -> None:
        key = "f" * 64
        answer = behavior_check.Answered("Q2", "owner", behavior_check.Approval.NO, "No", datetime.now(timezone.utc))
        behavior_check.write_record(key, answer)
        self.assertEqual(behavior_check.read_record(key), answer)


    def test_the_same_test_edit_has_a_different_key_for_other_code_or_origin(self) -> None:
        test = self.repo / "test_fixture.py"
        _ = self.write(test, "def test_fixture():\n    assert 1\n")
        base = self.commit_files()
        _ = self.write(test, "def test_fixture():\n    assert 2\n")
        implementation = self.write(self.repo / "application.py", 'value = "a b"\n')
        first = self.changes(base, self.commit_files())
        _ = self.write(implementation, 'value = "ab"\n')
        head = self.commit_files()
        second = self.changes(base, head)
        self.assertEqual(first.tests, second.tests)
        self.assertNotEqual(first.key, second.key)
        _ = self.git("remote", "set-url", "origin", str(self.root / "other.git"))
        third = self.changes(base, head)
        self.assertEqual((second.tests, second.files), (third.tests, third.files))
        self.assertNotEqual(second.key, third.key)

    def test_edits_to_identical_code_blocks_have_distinct_keys(self) -> None:
        test = self.write(self.repo / "test_fixture.py", "def test_fixture():\n    assert 1\n")
        block = "a = 1\nb = 2\nc = 3\nvalue = 4\nd = 5\ne = 6\nf = 7\n"
        code = self.write(self.repo / "application.py", block * 2)
        base = self.commit_files()
        _ = self.write(test, "def test_fixture():\n    assert 2\n")
        edited = block.replace("value = 4", "value = 8")
        _ = self.write(code, edited + block)
        first = self.changes(base, self.commit_files())
        _ = self.write(code, block + edited)
        second = self.changes(base, self.commit_files())
        self.assertEqual(first.tests, second.tests)
        self.assertNotEqual(first.key, second.key)

    def test_the_same_blob_edits_have_different_keys_when_a_file_mode_changes(self) -> None:
        test = self.write(self.repo / "test_fixture.py", "def test_fixture():\n    assert 1\n")
        script = self.write(self.repo / "run.sh", "#!/bin/sh\necho before\n")
        plain_base = self.commit_files()
        script.chmod(0o755)
        executable_base = self.commit_files()
        _ = self.write(test, "def test_fixture():\n    assert 2\n")
        _ = self.write(script, "#!/bin/sh\necho after\n")
        executable_head = self.commit_files()
        script.chmod(0o644)
        plain_head = self.commit_files()
        executable = self.changes(executable_base, executable_head)
        for base, head in ((plain_base, executable_head), (executable_base, plain_head)):
            with self.subTest(base=base, head=head):
                changed = self.changes(base, head)
                self.assertEqual(changed.tests, executable.tests)
                self.assertNotEqual(changed.key, executable.key)

    def test_a_class_decorator_changes_each_enclosed_python_test(self) -> None:
        original = ("class Demo:\n    def test_first(self):\n        pass\n"
                    "    class Inner:\n        async def test_second(self):\n            pass\n")
        path = self.write(self.repo / "test_fixture.py", original)
        base = self.commit_files()
        decorator = '@unittest.skip(\n    "no longer runs",\n)\n'
        _ = self.write(path, decorator + original)
        tests = self.changes(base, self.commit_files()).tests
        self.assertEqual([test.test for test in tests], [
            "test_fixture.py::Demo.Inner.test_second", "test_fixture.py::Demo.test_first"])
        for test in tests:
            self.assertEqual(test.new, decorator + test.old)

    def test_a_conditional_rust_test_attribute_keeps_both_bodies_in_the_diff(self) -> None:
        original = """#[cfg_attr(all(test, feature = "x"), allow(dead_code), test)]
fn conditional() {
    assert!(true);
}
"""
        path = self.write(self.repo / "tests/fixture.rs", original)
        base = self.commit_files()
        updated = original.replace("true", "false")
        _ = self.write(path, updated)
        self.assertEqual(self.changes(base, self.commit_files()).tests, (
            behavior_check.TestChange("tests/fixture.rs::conditional", original, updated),))

    def test_a_renamed_test_file_keeps_the_edited_test_on_both_sides(self) -> None:
        old_test = "def test_fixture():\n    assert 1\n"
        new_test = old_test.replace("1", "2")
        other_test = "\n\ndef test_unchanged():\n    assert long_value == expected_value\n"
        source = self.write(self.repo / "test_before.py", old_test + other_test)
        base = self.commit_files()
        target = source.rename(source.with_name("test_after.py"))
        _ = self.write(target, new_test + other_test)
        self.assertEqual(self.changes(base, self.commit_files()).tests, (
            behavior_check.TestChange("test_before.py::test_fixture", old_test, new_test),))

    def test_a_rust_module_attribute_changes_each_nested_test(self) -> None:
        original = """#[cfg(test)]
mod tests {
    #![allow(dead_code)]
    #[test]
    fn first() {}
    #[cfg(test)]
    mod nested {
        #[test]
        fn second() {}
    }
}
"""
        path = self.write(self.repo / "src/lib.rs", original)
        base = self.commit_files()
        for before, after in (("#[cfg(test)]", "#[cfg(any())]"),
                              ("#![allow(dead_code)]", "#![cfg(any())]")):
            with self.subTest(attribute=before):
                _ = self.write(path, original.replace(before, after, 1))
                tests = self.changes(base, self.commit_files()).tests
                self.assertEqual([test.test for test in tests], [
                    "src/lib.rs::tests::first", "src/lib.rs::tests::nested::second"])
                for test in tests:
                    self.assertIn(before, test.old)
                    self.assertEqual(test.new, test.old.replace(before, after, 1))
                self.assertEqual(tests[1].old.count("#[cfg(test)]"), 2)

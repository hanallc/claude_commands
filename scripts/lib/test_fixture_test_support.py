"""Test cleanup for nested unittest fixtures."""

from contextlib import ExitStack
from typing import override
import unittest

from scripts.lib.fixture_test_support import (
    clean_up_fixture,
    register_fixture_class_cleanup,
    set_up_fixture,
)


def raise_cleanup_error(error: BaseException) -> None:
    raise error


class FixtureCleanupTests(unittest.TestCase):
    def test_cleanup_keeps_each_exception_and_runs_all_callbacks(self) -> None:
        fixture = unittest.TestCase()
        completed: list[str] = []
        first = ValueError("first cleanup")
        second = OSError("second cleanup")
        fixture.addCleanup(completed.append, "last callback")
        fixture.addCleanup(raise_cleanup_error, second)
        fixture.addCleanup(raise_cleanup_error, first)

        with self.assertRaises(ExceptionGroup) as caught:
            clean_up_fixture(fixture)

        self.assertEqual(caught.exception.message, "TestCase cleanup failed")
        self.assertEqual(caught.exception.exceptions, (first, second))
        self.assertEqual(completed, ["last callback"])

    def test_instance_interrupt_preserves_errors_and_runs_all_callbacks(self) -> None:
        fixture = unittest.TestCase()
        completed: list[str] = []
        first = ValueError("first cleanup")
        interrupt = KeyboardInterrupt("interrupt cleanup")
        later = OSError("later cleanup")
        fixture.addCleanup(completed.append, "last callback")
        fixture.addCleanup(raise_cleanup_error, later)
        fixture.addCleanup(raise_cleanup_error, interrupt)
        fixture.addCleanup(raise_cleanup_error, first)

        with self.assertRaises(KeyboardInterrupt) as caught:
            clean_up_fixture(fixture)

        self.assertIs(caught.exception, interrupt)
        self.assertEqual(completed, ["last callback"])
        errors = caught.exception.__cause__
        assert isinstance(errors, ExceptionGroup)
        self.assertEqual(errors.exceptions, (first, later))

    def test_class_exit_preserves_errors_and_runs_all_callbacks(self) -> None:
        class Fixture(unittest.TestCase):
            pass

        completed: list[str] = []
        first = ValueError("first class cleanup")
        exit_error = SystemExit("exit class cleanup")
        later = OSError("later class cleanup")
        Fixture.addClassCleanup(completed.append, "last callback")
        Fixture.addClassCleanup(raise_cleanup_error, later)
        Fixture.addClassCleanup(raise_cleanup_error, exit_error)
        Fixture.addClassCleanup(raise_cleanup_error, first)

        with self.assertRaises(SystemExit) as caught:
            with ExitStack() as scope:
                _ = register_fixture_class_cleanup(scope.callback, Fixture)

        self.assertIs(caught.exception, exit_error)
        self.assertEqual(completed, ["last callback"])
        errors = caught.exception.__cause__
        assert isinstance(errors, ExceptionGroup)
        self.assertEqual(errors.exceptions, (first, later))

    def test_setup_and_cleanup_errors_reach_the_outer_result(self) -> None:
        class Fixture(unittest.TestCase):
            @override
            def setUp(self) -> None:
                self.addCleanup(raise_cleanup_error, ValueError("instance cleanup error"))
                raise RuntimeError("instance setup error")

        class OuterCase(unittest.TestCase):
            @override
            def setUp(self) -> None:
                _ = set_up_fixture(self.addCleanup, Fixture())

            def check_fixture(self) -> None:
                self.fail("The setup error must prevent the test.")

        result = unittest.TestResult()
        _ = OuterCase("check_fixture").run(result)
        setup_error, cleanup_error = [detail for _, detail in result.errors]
        self.assertIn("RuntimeError: instance setup error", setup_error)
        self.assertIn("ExceptionGroup: Fixture cleanup failed", cleanup_error)
        self.assertIn("ValueError: instance cleanup error", cleanup_error)

    def test_nested_class_errors_reach_the_outer_suite(self) -> None:
        class Fixture(unittest.TestCase):
            @classmethod
            @override
            def setUpClass(cls) -> None:
                cls.addClassCleanup(raise_cleanup_error, ValueError("class cleanup error"))
                cls.addClassCleanup(raise_cleanup_error, OSError("second class cleanup error"))
                raise RuntimeError("class setup error")

        class OuterCase(unittest.TestCase):
            @classmethod
            @override
            def setUpClass(cls) -> None:
                register_fixture_class_cleanup(cls.addClassCleanup, Fixture).setUpClass()

            def check_fixture(self) -> None:
                self.fail("The class setup error must prevent the test.")

        result = unittest.TestResult()
        _ = unittest.TestSuite([OuterCase("check_fixture")]).run(result)
        setup_error, cleanup_error = [detail for _, detail in result.errors]
        self.assertIn("RuntimeError: class setup error", setup_error)
        self.assertIn("ExceptionGroup: Fixture class cleanup failed", cleanup_error)
        self.assertIn("ValueError: class cleanup error", cleanup_error)
        self.assertIn("OSError: second class cleanup error", cleanup_error)

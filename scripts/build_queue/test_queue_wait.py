"""Check queue names and package parser retries."""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from scripts.build_queue import queue_wait
from scripts.lib.build_steps import RECORDED_STEP_NAMES


class VerifyPackageTests(unittest.TestCase):
    def test_failed_parse_retries_and_caches_the_next_package(self) -> None:
        cases = (
            ("timeout-package", subprocess.TimeoutExpired("bash", 2)),
            ("os-error-package", OSError("cannot start bash")),
            ("failed-exit-package", subprocess.CompletedProcess(["bash"], 2, stdout="partial", stderr="")),
            ("failed-extraction-package", AssertionError("cannot extract the parser")),
        )
        for package, failure in cases:
            success = subprocess.CompletedProcess(["bash"], 0, stdout=package, stderr="")
            with (self.subTest(package=package),
                  mock.patch.object(queue_wait, "verify_package_parser", return_value="") as extract,
                  mock.patch.object(subprocess, "run", return_value=success) as run):
                if isinstance(failure, AssertionError):
                    parse = extract
                    parse.side_effect = (failure, "")
                else:
                    parse = run
                    parse.side_effect = (failure, success)
                arguments = ("--filter", "name with spaces", package)
                self.assertEqual(queue_wait.verify_package("test", arguments), "")
                self.assertEqual(queue_wait.verify_package("test", arguments), package)
                self.assertEqual(queue_wait.verify_package("test", arguments), package)
                self.assertEqual(parse.call_count, 2)


class QueueRowNameTests(unittest.TestCase):
    def test_an_unknown_repository_uses_the_folder_name(self) -> None:
        self.assertEqual(queue_wait.QueueRowName.repository("unregistered session", "/private/widget"), "widget build")

    def test_queue_name_includes_the_package_after_a_parse_timeout(self) -> None:
        command = ("bash", "/fixture/verify.sh", "test", "retry-package")
        success = subprocess.CompletedProcess(["bash"], 0, stdout="retry-package", stderr="")
        with mock.patch.object(subprocess, "run", side_effect=(subprocess.TimeoutExpired("bash", 2), success)) as parse:
            self.assertEqual(queue_wait.command_queue_name(command, RECORDED_STEP_NAMES), "test")
            self.assertEqual(queue_wait.command_queue_name(command, RECORDED_STEP_NAMES), "test retry-package")
        self.assertEqual(parse.call_count, 2)

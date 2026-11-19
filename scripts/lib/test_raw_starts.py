"""Check test starts and the interpreter options that precede them."""

import unittest

from scripts.lib.raw_starts import is_test_start, python_target


class RawStartTests(unittest.TestCase):
    def test_test_starts(self) -> None:
        commands = (
            ("python3", "-m", "unittest", "suite"),
            ("python3", "-m", "unittest.__main__", "suite"),
            ("python3", "-Imunittest", "suite"),
            ("/bin/python3.13", "-W", "ignore", "-X", "dev", "--", "/repo/test_x.py"),
            ("scripts/lib/py", "-m", "scripts.lib.test_x"),
            ("scripts/lib/py", "-mpytest", "tests"),
            ("python3", "-m", "pytest.__main__", "tests"),
            ("pytest", "tests"),
            ("/venv/bin/py.test", "tests"),
            ("python3", "/venv/bin/pytest", "tests"),
            ("./test_x.py",),
            ("/bin/bash", "-e", "test_x.sh"),
            ("bash", "-O", "extglob", "--", "test_x.sh"),
            ("bash", "/repo/scripts/lib/py", "-m", "unittest"),
            ("node", "memory_page_proof.mjs"),
            ("node", "--require", "setup.cjs", "memory_page_proof.mjs"),
            ("node", "--import=./setup.mjs", "memory_page_proof.mjs"),
            ("node", "-r", "setup.cjs", "--", "memory_page_proof.mjs"),
            ("nodejs", "--env-file", "/tmp/config", "memory_page_proof.mjs"),
            ("python3", "scripts/test_times/module_run.py", "scripts/lib", "test_x.py"),
        )
        for argv in commands:
            with self.subTest(argv=argv):
                self.assertTrue(is_test_start(argv))

    def test_arguments_and_option_values_are_not_test_starts(self) -> None:
        commands = (
            (),
            ("python3", "helper.py", "-m", "unittest"),
            ("python3", "-c", "print('pytest')", "test_x.py"),
            ("python3", "-cprint(1)", "test_x.py"),
            ("python3", "-", "test_x.py"),
            ("python3", "-W", "test_x.py", "helper.py"),
            ("python3", "-m"),
            ("python3", "aggregate.py"),
            ("bash", "-lc", "bash test_x.sh"),
            ("bash", "-O", "test_x.sh", "helper.sh"),
            ("node", "--require", "memory_page_proof.mjs", "helper.mjs"),
            ("node", "--import", "memory_page_proof.mjs"),
            ("node", "--eval", "console.log(1)", "memory_page_proof.mjs"),
            ("node", "helper.mjs", "memory_page_proof.mjs"),
            ("grep", "test_"),
        )
        for argv in commands:
            with self.subTest(argv=argv):
                self.assertFalse(is_test_start(argv))

    def test_python_target_preserves_entry_point_arguments(self) -> None:
        cases = (
            (("-I", "-munittest", "suite"), ("-m", "unittest", "suite")),
            (("-W", "ignore", "-X", "dev", "--", "gate.py", "--tests-only"), ("gate.py", "--tests-only")),
            (("--check-hash-based-pycs", "always", "-Xdev", "helper.py", "-m", "unittest"),
             ("helper.py", "-m", "unittest")),
            (("-B", "-"), ("-",)),
            (("-I",), ()),
        )
        for arguments, target in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(python_target(arguments), target)


if __name__ == "__main__":
    _ = unittest.main()

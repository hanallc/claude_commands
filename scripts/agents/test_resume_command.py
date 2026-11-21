"""The `resume` command starts shutdown.py's resume verb from a link on PATH."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from scripts.lib.helper_tree_test_support import copy_helpers

COMMAND = Path(__file__).resolve().with_name("resume")


class ResumeCommandTests(unittest.TestCase):
    def test_a_fixture_home_write_does_not_change_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            source = COMMAND.parents[1] / "lib/package_root.py"
            before = source.read_bytes()
            scripts = copy_helpers(home)
            target = scripts / "lib/package_root.py"
            _ = target.write_text("private fixture replacement\n")
            self.assertTrue(target.resolve().is_relative_to(home.resolve()))
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(any(path.is_symlink() for path in (home / ".claude").rglob("*")))

    def test_a_linked_command_prints_the_resume_usage(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            _ = copy_helpers(home)
            link = home / "resume"
            link.symlink_to(COMMAND)
            result = subprocess.run([str(link), "--help"], cwd=folder, capture_output=True, text=True,
                                    timeout=60, env={**os.environ, "HOME": str(home), "HERDR_BIN": "/nonexistent/herdr"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--why", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()

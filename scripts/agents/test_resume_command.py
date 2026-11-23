"""The `resume` command starts shutdown.py's resume verb from a link on PATH."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from scripts.lib.helper_tree_test_support import copy_helpers
from scripts.herdr.missing_binary import MISSING_HERDR_BIN

COMMAND = Path(__file__).resolve().with_name("resume")


class ResumeCommandTests(unittest.TestCase):
    def test_a_fixture_home_write_does_not_change_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            source = COMMAND.parents[1] / "lib/python_startup/sitecustomize.py"
            before = source.read_bytes()
            scripts = copy_helpers(home)
            alias = scripts / "lib/session_python_startup"
            target = alias / "sitecustomize.py"
            replacement = "private fixture replacement\n"
            _ = target.write_text(replacement)
            self.assertEqual(alias.readlink(), Path("python_startup"))
            self.assertEqual((scripts / "lib/python_startup/sitecustomize.py").read_text(), replacement)
            self.assertEqual(source.read_bytes(), before)

    def test_a_linked_command_prints_the_resume_usage(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            _ = copy_helpers(home)
            link = home / "resume"
            link.symlink_to(COMMAND)
            result = subprocess.run([str(link), "--help"], cwd=folder, capture_output=True, text=True,
                                    timeout=60, env={**os.environ, "HOME": str(home), "HERDR_BIN": MISSING_HERDR_BIN})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--why", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()

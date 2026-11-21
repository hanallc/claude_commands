"""Check the repository hook at interpreter startup."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class PythonStartupTests(unittest.TestCase):
    def test_chained_hook_and_callback_do_not_import_typing(self) -> None:
        startup = Path(__file__).with_name("python_startup").resolve()
        with tempfile.TemporaryDirectory() as temporary:
            following = Path(temporary)
            _ = (following / "sitecustomize.py").write_text(
                "from sitecustomize import after_startup\n"
                + "after_startup(lambda: print('callback ran'))\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-c", "import sys; print('typing' in sys.modules)"],
                cwd=startup.parents[2],
                env={"PYTHONPATH": os.pathsep.join((str(startup), str(following))), "PYTHONNOUSERSITE": "1"},
                capture_output=True,
                text=True,
                timeout=TEST_HANG_GUARD_SECS,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.splitlines(), ["callback ran", "False"])

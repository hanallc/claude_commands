from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import final
from unittest.mock import patch


from scripts.prioritize import writer_lock


@final
class WriterLockTests(unittest.TestCase):
    def test_lock_is_exclusive_across_processes_and_released_on_exit(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"PRIORITIZE_WRITER_LOCK_ROOT": directory}),
        ):
            code = "\n".join(
                (
                    "from scripts.prioritize import writer_lock",
                    "try:",
                    "    with writer_lock.acquire_writer_lock(timeout_seconds=0.05):",
                    "        pass",
                    "except writer_lock.WriterLockError:",
                    "    raise SystemExit(7)",
                )
            )

            with writer_lock.acquire_writer_lock():
                self.assertTrue((Path(directory) / "writer.lock").is_file())
                self.assertTrue(writer_lock.lock_is_held())
                contender = subprocess.run(
                    [sys.executable, "-c", code],
                    check=False,
                    capture_output=True,
                    text=True,
                )

            self.assertEqual(contender.returncode, 7, contender.stderr)
            self.assertFalse(writer_lock.lock_is_held())
            successor = subprocess.run(
                [sys.executable, "-c", code],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(successor.returncode, 0, successor.stderr)


if __name__ == "__main__":
    _ = unittest.main()

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import final


from scripts.prioritize import writer_lock


@final
class WriterLockTests(unittest.TestCase):
    def test_lock_is_exclusive_across_processes_and_released_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "writer.lock"
            code = "\n".join(
                (
                    "import sys",
                    "from pathlib import Path",
                    "from scripts.prioritize import writer_lock",
                    "try:",
                    "    with writer_lock.acquire_writer_lock(Path(sys.argv[1]), timeout_seconds=0.05):",
                    "        pass",
                    "except writer_lock.WriterLockError:",
                    "    raise SystemExit(7)",
                )
            )

            with writer_lock.acquire_writer_lock(lock_path):
                self.assertTrue(lock_path.is_file())
                self.assertTrue(writer_lock.lock_is_held(lock_path))
                contender = subprocess.run(
                    [sys.executable, "-c", code, str(lock_path)],
                    check=False,
                    capture_output=True,
                    text=True,
                )

            self.assertEqual(contender.returncode, 7, contender.stderr)
            self.assertFalse(writer_lock.lock_is_held(lock_path))
            successor = subprocess.run(
                [sys.executable, "-c", code, str(lock_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(successor.returncode, 0, successor.stderr)


if __name__ == "__main__":
    _ = unittest.main()

"""A long temporary root cannot lengthen a fixture's Unix socket path."""
from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib import build_folders
from scripts.lib.socket_test_support import PARENT, short_socket_directory
from scripts.lint.memory_test_support import config_environment


class ShortSocketDirectoryTests(unittest.TestCase):
    def test_socket_parent_uses_current_build_folder_configuration(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            config = root / "build_folders.conf"
            _ = config_environment(root)
            original = config.read_text()
            with patch.dict(os.environ, {"CONF_DIR": str(root)}):
                for name in ("first", "second"):
                    parent = root / name
                    parent.mkdir()
                    _ = config.write_text(original.replace("roots.system=/tmp", f"roots.system={parent}"))
                    with short_socket_directory() as socket_directory:
                        path = Path(socket_directory)
                        self.assertEqual(path.parent, parent / PARENT)
                        with socket.socket(socket.AF_UNIX) as listener:
                            listener.bind(str(path / "s"))
                    self.assertFalse(path.exists())

    def test_socket_binds_with_long_tmpdir_and_private_folder_is_removed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as root:
            long_tmpdir = Path(root) / ("long-session-" * 8)
            long_tmpdir.mkdir()
            self.assertGreater(len(os.fsencode(long_tmpdir)), 108)
            with patch.dict(os.environ, {"TMPDIR": str(long_tmpdir)}), patch.object(tempfile, "tempdir", None):
                self.assertEqual(tempfile.gettempdir(), str(long_tmpdir))
                with short_socket_directory() as directory:
                    folder = Path(directory)
                    self.assertEqual(folder.parent, Path("/tmp") / PARENT)
                    self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
                    path = folder / "s"
                    self.assertLess(len(os.fsencode(path)), 108)
                    with socket.socket(socket.AF_UNIX) as listener:
                        listener.bind(str(path))
                        self.assertTrue(path.is_socket())
                self.assertFalse(folder.exists())

    def test_a_new_folder_removes_the_free_folders_a_killed_run_left(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            config = root / "build_folders.conf"
            _ = config_environment(root)
            _ = config.write_text(config.read_text().replace("roots.system=/tmp", f"roots.system={root}"))
            killed = ("from scripts.lib.socket_test_support import short_socket_directory\n"
                      "import os\nfolder = short_socket_directory()\nprint(folder.name, flush=True)\nos._exit(0)\n")
            with patch.dict(os.environ, {"CONF_DIR": str(root)}):
                left = Path(subprocess.run([sys.executable, "-c", killed], check=True, capture_output=True,
                                           text=True, cwd=Path(__file__).resolve().parents[2]).stdout.strip())
                self.assertTrue(left.is_dir())
                with short_socket_directory() as held:
                    self.assertFalse(left.exists())
                    self.assertFalse(left.with_name(left.name + ".lease.json").exists())
                    _ = build_folders.sweep_temporary_build_folders(Path(held).parent, wait=False)
                    self.assertTrue(Path(held).is_dir())

    def test_one_process_sweeps_a_parent_once(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            config = root / "build_folders.conf"
            _ = config_environment(root)
            _ = config.write_text(config.read_text().replace("roots.system=/tmp", f"roots.system={root}"))
            with (patch.dict(os.environ, {"CONF_DIR": str(root)}),
                  patch.object(build_folders, "sweep_temporary_build_folders") as sweep):
                for _ in range(3):
                    with short_socket_directory():
                        pass
            sweep.assert_called_once_with(root / PARENT, wait=False)


if __name__ == "__main__":
    _ = unittest.main()

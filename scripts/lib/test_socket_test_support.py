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
from typing import override
from unittest.mock import patch

from scripts.lib import build_folders
from scripts.lib.socket_test_support import PARENT, short_socket_directory
from scripts.lint.memory_test_support import config_environment, write_config_fixture


class ShortSocketDirectoryTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.root: Path = self.enterContext(build_folders.temporary_build_folder(
            build_folders.UniqueBuildFolder("socket-", build_folders.ExplicitBuildParent(build_folders.build_root()))))
        self.configuration: Path = self.root / "config"
        _ = config_environment(self.configuration)
        _ = self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(self.configuration)}))
        self.set_socket_root(self.root)

    def set_socket_root(self, root: Path) -> None:
        host = socket.gethostname().partition(".")[0]
        _ = write_config_fixture(self.configuration / "build_folders.conf",
                                 f"roots.build={root}\nroots.build.{host}={root}\n")

    def test_socket_parent_uses_current_build_folder_configuration(self) -> None:
        for name in ("first", "second"):
            parent = self.root / name
            parent.mkdir()
            self.set_socket_root(parent)
            with short_socket_directory() as socket_directory:
                path = Path(socket_directory)
                self.assertEqual(path.parent, parent / PARENT)
                with socket.socket(socket.AF_UNIX) as listener:
                    listener.bind(str(path / "s"))
            self.assertFalse(path.exists())

    def test_socket_binds_with_long_tmpdir_and_private_folder_is_removed(self) -> None:
        long_tmpdir = self.root / ("long-session-" * 8)
        long_tmpdir.mkdir()
        self.assertGreater(len(os.fsencode(long_tmpdir)), 104)
        with (patch.dict(os.environ, {"TMPDIR": str(long_tmpdir)}), patch.object(tempfile, "tempdir", None)):
            self.assertEqual(tempfile.gettempdir(), str(long_tmpdir))
            with short_socket_directory() as directory:
                folder = Path(directory)
                self.assertEqual(folder.parent, self.root / PARENT)
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
                path = folder / "admission.sock"
                self.assertLess(len(os.fsencode(path)), 104)
                with socket.socket(socket.AF_UNIX) as listener:
                    listener.bind(str(path))
                    self.assertTrue(path.is_socket())
            self.assertFalse(folder.exists())

    def test_a_new_folder_removes_the_free_folders_a_killed_run_left(self) -> None:
        killed = ("from scripts.lib.socket_test_support import short_socket_directory\n"
                  "import os\nfolder = short_socket_directory()\nprint(folder.name, flush=True)\nos._exit(0)\n")
        left = Path(subprocess.run([sys.executable, "-c", killed], check=True, capture_output=True,
                                   text=True, cwd=Path(__file__).resolve().parents[2]).stdout.strip())
        self.assertTrue(left.is_dir())
        with short_socket_directory() as held:
            self.assertFalse(left.exists())
            self.assertFalse(left.with_name(left.name + ".lease.json").exists())
            build_folders.sweep_temporary_build_folders(Path(held).parent, wait=False)
            self.assertTrue(Path(held).is_dir())

    def test_one_process_sweeps_a_parent_once(self) -> None:
        with patch.object(build_folders, "sweep_temporary_build_folders") as sweep:
            for _ in range(3):
                with short_socket_directory():
                    pass
        sweep.assert_called_once_with(self.root / PARENT, wait=False)


if __name__ == "__main__":
    _ = unittest.main()

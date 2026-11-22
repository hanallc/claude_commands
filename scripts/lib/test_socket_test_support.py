"""Test the socket folder routes and cleanup."""
from __future__ import annotations

import os
import socket
import stat
import tempfile
import unittest
from contextlib import chdir, nullcontext
from pathlib import Path
from unittest.mock import patch

from scripts.lib import socket_test_support
from scripts.lib.socket_test_support import UNIX_SOCKET_PATH_LIMIT, short_socket_directory


class ShortSocketDirectoryTests(unittest.TestCase):
    def test_socket_folder_uses_a_short_tmpdir(self) -> None:
        with (tempfile.TemporaryDirectory(prefix="t") as tmpdir, chdir(tmpdir),
              patch.dict(os.environ, {"TMPDIR": "."}),
              patch.object(tempfile, "tempdir", ".")):
            with short_socket_directory() as directory:
                folder = Path(directory)
                self.assertEqual(folder.resolve().parent, Path(tmpdir).resolve())
                with socket.socket(socket.AF_UNIX) as listener:
                    listener.bind(str(folder / "s"))
            self.assertFalse(folder.exists())

    def test_daemon_socket_uses_fallback_with_a_long_spaced_tmpdir(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            long_tmpdir = Path(root) / "long tmpdir".ljust(99 - len(os.fsencode(root)), "x")
            long_tmpdir.mkdir()
            fallback = Path("fallback")
            with (chdir(root), patch.object(socket_test_support, "FALLBACK_SOCKET_ROOT", fallback),
                  patch.dict(os.environ, {"TMPDIR": str(long_tmpdir)}),
                  patch.object(tempfile, "tempdir", None)):
                fallback.mkdir()
                self.assertEqual(tempfile.gettempdir(), str(long_tmpdir))
                for fail in (False, True):
                    with self.subTest(fail=fail):
                        outcome = self.assertRaisesRegex(RuntimeError, "fixture failure") if fail else nullcontext()
                        with outcome, short_socket_directory() as directory:
                            folder = Path(directory)
                            self.assertEqual(folder.parent, fallback)
                            self.bind_daemon_socket(folder)
                            if fail:
                                raise RuntimeError("fixture failure")
                        self.assertFalse(folder.exists())

    def bind_daemon_socket(self, folder: Path) -> None:
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        path = folder / ".codex2/app-server-control/app-server-control.sock"
        path.parent.mkdir(parents=True)
        self.assertLessEqual(len(os.fsencode(path)), UNIX_SOCKET_PATH_LIMIT)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            self.assertTrue(path.is_socket())


if __name__ == "__main__":
    _ = unittest.main()

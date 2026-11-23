"""Tests for the live configuration guard."""
from __future__ import annotations

import os
import pwd
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import final
from unittest.mock import patch

from scripts.lib import live_config_guard
from scripts.lint.memory_test_support import write_config_fixture

GUARD = Path(live_config_guard.__file__).resolve()

PROBE = textwrap.dedent("""
    import os, runpy, sys
    from pathlib import Path

    guard = runpy.run_path(sys.argv[1])
    live, outside = Path(sys.argv[2]), Path(sys.argv[3])
    guard["install"](live)

    def attempt(label, action):
        try:
            action()
        except guard["LiveConfigWrite"]:
            print(label, "refused")
        else:
            print(label, "allowed")

    attempt("write_text", lambda: (live / "new.conf").write_text("a=1\\n"))
    attempt("append", lambda: open(live / "console.conf", "a").close())
    attempt("os.open", lambda: os.close(os.open(live / "raw.conf", os.O_WRONLY | os.O_CREAT)))
    attempt("replace", lambda: os.replace(outside / "staged.conf", live / "console.conf"))
    attempt("remove", lambda: os.remove(live / "console.conf"))
    attempt("link", lambda: (outside / "link").write_text("a=1\\n"))
    attempt("read", lambda: (live / "console.conf").read_text())
    attempt("outside", lambda: (outside / "free.conf").write_text("a=1\\n"))
    print("console", (live / "console.conf").read_text().strip())
""")


@final
class LiveConfigGuardTests(unittest.TestCase):
    def folders(self) -> tuple[Path, Path]:
        """Return a private root and a stand-in live folder that holds one setting."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        live = root / "live"
        live.mkdir()
        _ = (live / "console.conf").write_text("page.list.owner=live\n", encoding="utf-8")
        return root, live

    def test_live_folder_ignores_a_test_home(self) -> None:
        root, _ = self.folders()
        with patch.dict(os.environ, {"HOME": str(root)}):
            folder = live_config_guard.live_folder()
        self.assertEqual(folder, Path(pwd.getpwuid(os.getuid()).pw_dir, ".claude", "config").resolve())

    def test_fixture_writer_refuses_the_live_folder(self) -> None:
        _, live = self.folders()
        with (patch.object(live_config_guard, "live_folder", return_value=live),
              self.assertRaisesRegex(live_config_guard.LiveConfigWrite, "set CONF_DIR to a fixture folder")):
            _ = write_config_fixture(live / "console.conf", "page.list.owner=producer-staff\n")
        self.assertEqual((live / "console.conf").read_text(encoding="utf-8"), "page.list.owner=live\n")

    def test_fixture_writer_refuses_a_link_into_the_live_folder(self) -> None:
        root, live = self.folders()
        link = root / "fixture"
        link.symlink_to(live)
        with (patch.object(live_config_guard, "live_folder", return_value=live),
              self.assertRaises(live_config_guard.LiveConfigWrite)):
            _ = write_config_fixture(link / "console.conf", "page.list.owner=producer-staff\n")
        self.assertEqual((live / "console.conf").read_text(encoding="utf-8"), "page.list.owner=live\n")

    def test_fixture_writer_writes_a_fixture_folder(self) -> None:
        root, live = self.folders()
        with patch.object(live_config_guard, "live_folder", return_value=live):
            _ = write_config_fixture(root / "fixture" / "console.conf", "page.list.owner=fixture\n")
        self.assertIn("page.list.owner=fixture", (root / "fixture" / "console.conf").read_text(encoding="utf-8"))

    def test_installed_guard_refuses_each_change_and_allows_reads(self) -> None:
        root, live = self.folders()
        outside = root / "outside"
        outside.mkdir()
        _ = (outside / "staged.conf").write_text("page.list.owner=staged\n", encoding="utf-8")
        (outside / "link").symlink_to(live / "linked.conf")
        result = subprocess.run([sys.executable, "-I", "-c", PROBE, str(GUARD), str(live), str(outside)],
                                capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "write_text refused", "append refused", "os.open refused", "replace refused", "remove refused",
            "link refused", "read allowed", "outside allowed", "console page.list.owner=live"])
        self.assertEqual(sorted(path.name for path in live.iterdir()), ["console.conf"])


if __name__ == "__main__":
    _ = unittest.main()

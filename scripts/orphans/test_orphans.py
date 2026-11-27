"""Check the orphan scan with a private file-size limit."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture
from scripts.orphans import orphans


class OrphansTests(unittest.TestCase):
    def test_corpus_reads_the_size_limit_on_each_scan(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = root / "settings"
        settings = config / "orphans.conf"
        candidate = root / "CLAUDE.md"
        _ = candidate.write_text("éé", encoding="utf-8")
        with patch.dict(os.environ, {"CONF_DIR": str(config)}):
            _ = write_config_fixture(settings, "corpus.max_file_bytes=3\n")
            self.assertEqual(orphans.read_reference_corpus(root), {})
            _ = write_config_fixture(settings, "corpus.max_file_bytes=4\n")
            self.assertEqual(orphans.read_reference_corpus(root), {candidate: "éé"})

    def test_cli_reports_an_unusable_size_limit(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        scan_root = root / ".claude"
        scan_root.mkdir()
        config = root / "settings"
        _ = write_config_fixture(config / "orphans.conf", "corpus.max_file_bytes=0\n")
        result = subprocess.run(
            [sys.executable, str(Path(orphans.__file__).resolve()), str(scan_root)],
            cwd=root, env={**os.environ, "CONF_DIR": str(config)},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("corpus.max_file_bytes", result.stderr)
        self.assertIn("outside range 1..", result.stderr)
        self.assertEqual(result.stdout, "")

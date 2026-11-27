"""Check the wait limits and the message at the limit."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.lib import conf, waits


class WaitTests(unittest.TestCase):
    def test_each_kind_and_size_reads_its_value(self) -> None:
        expected = {
            waits.WaitKind.LOCK: 30,
            waits.WaitKind.NETWORK_CALL: 30,
            waits.WaitKind.STOP_GRACE: 5,
            waits.WaitKind.STALL_ALERT: 300,
            waits.WaitSize.FAST: 8,
            waits.WaitSize.SHORT: 30,
            waits.WaitSize.STANDARD: 120,
            waits.WaitSize.MEDIUM: 300,
            waits.WaitSize.LONG: 900,
            waits.WaitSize.EXTENDED: 1800,
            waits.WaitSize.MAXIMUM: 7200,
            waits.OwnWait.STEVIE_POOL: 0.5,
        }
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, CONF_DIR=folder):
            self.assertEqual(set(expected), {*waits.WaitKind, *waits.WaitSize, *waits.OwnWait})
            for key, seconds in expected.items():
                with self.subTest(key=key):
                    self.assertEqual(waits.read(key), waits.WaitLimit(key, seconds))

    def test_missing_key_fails(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, CONF_DIR=folder):
            _ = (Path(folder) / "waits.conf").write_text("# whole: 1..\nsize.short.seconds=30\n")
            with self.assertRaisesRegex(conf.Unusable, r"size\.fast\.seconds"):
                _ = waits.read(waits.WaitSize.FAST)

    def test_limit_message_names_the_operation_seconds_and_key(self) -> None:
        limit = waits.WaitLimit(waits.OwnWait.STEVIE_POOL, 0.5)
        self.assertEqual(limit.limit_message("the Stevie token pool"),
                         "Waited 0.5 seconds for the Stevie token pool (waits.conf stevie_pool.seconds).")

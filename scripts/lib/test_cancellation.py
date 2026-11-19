"""Check the signals shared by the cancellation callers."""
from __future__ import annotations

import signal
import unittest

from scripts.lib import cancellation


class CancellationTests(unittest.TestCase):
    def test_cancellation_signals_match_the_child_reset(self) -> None:
        self.assertEqual(cancellation.CANCELLATION_SIGNALS, (signal.SIGTERM, signal.SIGHUP, signal.SIGINT))

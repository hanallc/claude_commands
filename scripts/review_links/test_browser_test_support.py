"""Test browser cleanup after SIGKILL stops the test process."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import unittest
from contextlib import suppress
from pathlib import Path

from scripts.lib.process_start import Gone, Started, Zombie, proc_table_start


START_BROWSER = """
import subprocess
import sys

from scripts.review_links.browser_test_support import start_browser

stand_in = "import os, time; print(os.getpid(), flush=True); os.close(1); time.sleep(600)"
browser = start_browser([sys.executable, "-c", stand_in], output=bytes, stderr=subprocess.DEVNULL)
sys.exit(browser.process.wait())
"""


class BrowserTestSupportTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "macOS has no parent-death signal.")
    def test_killing_the_test_stops_the_browser(self) -> None:
        proc = Path("/proc")
        with (subprocess.Popen([sys.executable, "-c", START_BROWSER], stdout=subprocess.PIPE, text=False, bufsize=0) as owner,
              selectors.DefaultSelector() as ready):
            output = owner.stdout
            assert output is not None
            _ = ready.register(output, selectors.EVENT_READ)
            try:
                self.assertTrue(ready.select(10), "The stand-in did not report its PID.")
                pid = int(os.read(output.fileno(), 64))
                started = proc_table_start(pid, proc)
                assert isinstance(started, Started), started
                descriptor = os.pidfd_open(pid)
                try:
                    self.assertEqual(proc_table_start(pid, proc), started)
                    try:
                        _ = ready.unregister(output)
                        _ = ready.register(descriptor, selectors.EVENT_READ)
                        owner.kill()
                        self.assertTrue(ready.select(10), "The stand-in survived SIGKILL of its parent.")
                        self.assertEqual(owner.wait(timeout=10), -signal.SIGKILL)
                        ended = proc_table_start(pid, proc)
                        if isinstance(ended, Zombie):
                            self.assertEqual(ended, Zombie(started.start))
                        elif isinstance(ended, Started):
                            self.assertNotEqual(ended, started)
                        else:
                            self.assertIsInstance(ended, Gone)
                    finally:
                        with suppress(ProcessLookupError):
                            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                finally:
                    os.close(descriptor)
            finally:
                if owner.poll() is None:
                    owner.kill()

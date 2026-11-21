#!/usr/bin/env python3
"""mac_wake.py: one watcher sends a queued Mac pull as soon as the Mac answers."""
from __future__ import annotations

import fcntl
import io
import os
import platform
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import override
from unittest import mock

from scripts.lib import mac_wake, main_publish


def _done(code: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["ssh"], code, "", "")


class MacWakeTests(unittest.TestCase):
    root: Path = Path()
    launched: list[tuple[list[str], bool]] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("MAC_WAKE_PROGRAM", "ZONE_SSH")}
        environment["ZONE_STATE"] = str(self.root / "zone")
        self.enterContext(mock.patch.dict(os.environ, environment, clear=True))
        _ = self.enterContext(mock.patch.object(platform, "system", return_value="Linux"))
        self.launched = []

        def launch(arguments: list[str], *, start_new_session: bool = False, **_: object) -> None:
            self.launched.append((arguments, start_new_session))

        _ = self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=launch))

    def queue_pull(self) -> None:
        main_publish.queue_pull_only("tune: accounts.conf", "ssh: connect to host mac port 22: No route to host")

    def test_start_runs_one_watcher_when_a_pull_waits(self) -> None:
        self.queue_pull()
        os.environ["MAC_WAKE_PROGRAM"] = str(self.root / "watcher.py")
        self.assertEqual(mac_wake.start(), ["mac: a watcher sends the waiting pull as soon as the Mac answers"])
        self.assertEqual([(arguments[1:], session) for arguments, session in self.launched],
                         [([str(self.root / "watcher.py"), "watch"], True)])

    def test_start_runs_no_watcher_when_nothing_waits(self) -> None:
        os.environ["MAC_WAKE_PROGRAM"] = str(self.root / "watcher.py")
        self.assertEqual(mac_wake.start(), [])
        self.assertEqual(self.launched, [])

    def test_start_runs_no_watcher_for_a_fixture_queue(self) -> None:
        self.queue_pull()
        self.assertEqual(mac_wake.start(), [])
        self.assertEqual(self.launched, [])

    def test_start_runs_no_watcher_while_one_holds_the_lock(self) -> None:
        self.queue_pull()
        os.environ["MAC_WAKE_PROGRAM"] = str(self.root / "watcher.py")
        with mac_wake.lock_path().open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(mac_wake.start(), [])
        self.assertEqual(self.launched, [])

    def test_the_mac_answers_only_when_the_probe_exits_zero(self) -> None:
        for result, expected in ((_done(0), True), (_done(255), False), ("timed out after 5 seconds", False)):
            with self.subTest(result=result), mock.patch.object(main_publish, "ssh", return_value=result) as probe:
                self.assertEqual(mac_wake._answers(), expected)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(probe.call_args.args, ("true",))
                self.assertEqual(probe.call_args.kwargs["time_limit"], main_publish.CommandTimeLimit(5))

    def test_watch_drains_when_the_mac_answers_after_it_slept(self) -> None:
        self.queue_pull()

        def drain(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            main_publish.queue_path().unlink()
            return subprocess.CompletedProcess(arguments, 0, "mac: pulled\n", "")

        with mock.patch.object(mac_wake, "_answers", side_effect=[False, True]) as probe, \
                mock.patch.object(subprocess, "run", side_effect=drain) as run, \
                mock.patch.object(time, "sleep") as pause, redirect_stdout(io.StringIO()) as shown:
            self.assertEqual(mac_wake.watch(), 0)
        self.assertEqual(shown.getvalue(), "mac: pulled\n")
        self.assertEqual(probe.call_count, 2)
        pause.assert_called_once_with(10)
        self.assertEqual(run.call_args.args[0][1:], [str(mac_wake.ZONE_SCRIPT), "drain"])

    def test_watch_probes_again_when_the_drain_leaves_a_pull(self) -> None:
        self.queue_pull()
        drained = [subprocess.CompletedProcess(["drain"], 1, "", ""),
                   subprocess.CompletedProcess(["drain"], 0, "", "")]

        def drain(*_arguments: object, **_options: object) -> subprocess.CompletedProcess[str]:
            if len(drained) == 1:
                main_publish.queue_path().unlink()
            return drained.pop(0)

        with mock.patch.object(mac_wake, "_answers", return_value=True), \
                mock.patch.object(subprocess, "run", side_effect=drain) as run, \
                mock.patch.object(time, "sleep") as pause:
            self.assertEqual(mac_wake.watch(), 0)
        self.assertEqual(run.call_count, 2)
        pause.assert_called_once_with(10)

    def test_a_second_watcher_exits_while_the_first_holds_the_lock(self) -> None:
        self.queue_pull()
        with mac_wake.lock_path().open("a") as held, mock.patch.object(mac_wake, "_answers") as probe:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(mac_wake.watch(), 0)
        probe.assert_not_called()


if __name__ == "__main__":
    _ = unittest.main()

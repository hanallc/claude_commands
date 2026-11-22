"""Check process stops with private process and scope records."""
from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch

from scripts.lib import process_stop


def write_process(root: Path, pid: int, start: int, *, cgroup: str = "") -> None:
    directory = root / str(pid)
    directory.mkdir()
    fields = ["S", "1", "701", "701", *(["0"] * 15), str(start)]
    _ = (directory / "stat").write_text(f"{pid} (test process) " + " ".join(fields))
    _ = (directory / "cgroup").write_text(f"0::{cgroup}\n")


@unittest.skipUnless(process_stop.supported(), "pidfd stop needs Linux")
class ProcessStopTests(unittest.TestCase):
    def test_a_reused_pid_is_never_signaled(self) -> None:
        with TemporaryDirectory() as folder:
            proc = Path(folder)
            leader = process_stop.ProcessLifetime(701, "11")
            write_process(proc, leader.pid, 22)
            with (patch.object(process_stop, "PROC", proc),
                  patch.object(os, "pidfd_open", return_value=47) as opened,
                  patch.object(os, "close") as closed,
                  patch.object(signal, "pidfd_send_signal") as send,
                  self.assertRaises(ProcessLookupError)):
                process_stop.stop_command_tree(leader, process_stop.NoScopeUnit(), signal.SIGTERM)
            opened.assert_called_once_with(701)
            closed.assert_called_once_with(47)
            send.assert_not_called()

    def test_scope_kill_requires_membership_in_the_open_directory(self) -> None:
        unit = process_stop.ScopeUnit("builds-detached.slice", "detached-fixture.scope")
        relative = f"/{unit.slice}/{unit.unit}"
        leader = process_stop.ProcessLifetime(701, "11")
        for members, expected_kill in (("702\n", "1\n"), ("704\n", "0\n")):
            with self.subTest(members=members), TemporaryDirectory() as folder, ExitStack() as cleanup:
                proc = Path(folder) / "proc"
                proc.mkdir()
                cgroups = Path(folder) / "cgroups"
                scope = cgroups / relative.lstrip("/")
                scope.mkdir(parents=True)
                _ = (scope / "cgroup.procs").write_text(members)
                kill_file = scope / "cgroup.kill"
                _ = kill_file.write_text("0\n")
                write_process(proc, 701, 11)
                write_process(proc, 702, 22, cgroup=relative)
                write_process(proc, 703, 33)
                descriptors: dict[int, int] = {}

                def open_pidfd(pid: int) -> int:
                    # Keep the pipe open to represent a process that has not exited.
                    read, write = os.pipe()
                    _ = cleanup.callback(os.close, write)
                    descriptors[pid] = read
                    return read

                with (patch.object(process_stop, "PROC", proc),
                      patch.object(process_stop, "CGROUP_ROOT", cgroups),
                      patch.object(os, "pidfd_open", side_effect=open_pidfd),
                      patch.object(signal, "pidfd_send_signal") as send):
                    process_stop.stop_command_tree(leader, unit, signal.SIGTERM)
                self.assertEqual(kill_file.read_text(), expected_kill)
                signaled = {701, 703} if members == "702\n" else {701, 702, 703}
                self.assertEqual(send.call_count, len(signaled))
                send.assert_has_calls([call(descriptors[pid], signal.SIGTERM) for pid in signaled],
                                      any_order=True)

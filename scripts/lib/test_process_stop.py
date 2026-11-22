"""Check process stops with private process and cgroup records."""
from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.lib import process_stop


LEADER = process_stop.ProcessLifetime(701, "11")


def write_process(root: Path, pid: int, start: int, *, session: int = 701) -> None:
    directory = root / str(pid)
    directory.mkdir()
    fields = ["S", "1", str(session), str(session), *(["0"] * 15), str(start)]
    _ = (directory / "stat").write_text(f"{pid} (test process) " + " ".join(fields))


class StopFixture:
    def __init__(self, folder: Path) -> None:
        self.proc: Path = folder / "proc"
        self.proc.mkdir()
        write_process(self.proc, LEADER.pid, int(LEADER.start))

    def stop(self, number: int) -> list[tuple[int, int]]:
        pids: dict[int, int] = {}
        signals: list[tuple[int, int]] = []
        with ExitStack() as cleanup:
            def open_pidfd(pid: int) -> int:
                read, write = os.pipe()
                _ = cleanup.callback(os.close, write)
                pids[read] = pid
                return read

            def record_signal(descriptor: int, signum: int) -> None:
                signals.append((pids[descriptor], signum))

            with (patch.object(process_stop, "PROC", self.proc),
                  patch.object(os, "pidfd_open", side_effect=open_pidfd),
                  patch.object(signal, "pidfd_send_signal", side_effect=record_signal)):
                process_stop.stop_group(LEADER, number)
        return signals


@unittest.skipUnless(process_stop.supported(), "pidfd stop needs Linux")
class ProcessStopTests(unittest.TestCase):
    def test_a_reused_pid_is_never_signaled(self) -> None:
        with TemporaryDirectory() as folder:
            proc = Path(folder)
            write_process(proc, LEADER.pid, 22)
            with (patch.object(process_stop, "PROC", proc),
                  patch.object(os, "pidfd_open", return_value=47) as opened,
                  patch.object(os, "close") as closed,
                  patch.object(signal, "pidfd_send_signal") as send,
                  self.assertRaises(ProcessLookupError)):
                process_stop.stop_group(LEADER, signal.SIGTERM)
            opened.assert_called_once_with(701)
            closed.assert_called_once_with(47)
            send.assert_not_called()

    def test_without_a_scope_term_reaches_the_original_group(self) -> None:
        with TemporaryDirectory() as folder:
            fixture = StopFixture(Path(folder))
            write_process(fixture.proc, 702, 22)
            write_process(fixture.proc, 703, 33, session=703)
            self.assertCountEqual(fixture.stop(signal.SIGTERM),
                                  [(701, signal.SIGTERM), (702, signal.SIGTERM)])

    def test_slice_term_checks_current_membership_and_process_identity(self) -> None:
        with TemporaryDirectory() as folder:
            root = Path(folder)
            fixture = StopFixture(root)
            cgroups = root / "cgroup"
            run_slice = cgroups / "builds.slice/builds-detached.slice/builds-detached-fixture.slice"
            step = run_slice / "step.scope"
            step.mkdir(parents=True)
            relative = "/" + str(run_slice.relative_to(cgroups))
            _ = (run_slice / "cgroup.procs").write_text("")
            _ = (step / "cgroup.procs").write_text("701\n702\n703\n")
            _ = (run_slice / "cgroup.events").write_text("populated 1\n")
            for pid in (701, 702, 703):
                if pid != 701:
                    write_process(fixture.proc, pid, pid)
                group = f"{relative}/{step.name}" if pid != 702 else "/elsewhere.scope"
                _ = (fixture.proc / str(pid) / "cgroup").write_text(f"0::{group}\n")
            stat = run_slice.stat()
            identity: process_stop.RunSliceIdentity = {
                "path": relative, "unit": run_slice.name, "device": stat.st_dev, "inode": stat.st_ino,
            }
            descriptors: dict[int, int] = {}
            signals: list[tuple[int, int]] = []

            def open_pidfd(pid: int) -> int:
                if pid == 703:
                    record = fixture.proc / str(pid) / "stat"
                    _ = record.write_text(record.read_text().removesuffix("703") + "704")
                descriptor = os.open(os.devnull, os.O_RDONLY)
                descriptors[descriptor] = pid
                return descriptor

            def send_term(descriptor: int, number: int) -> None:
                signals.append((descriptors[descriptor], number))
                _ = (run_slice / "cgroup.events").write_text("populated 0\n")

            with (patch.object(process_stop, "CGROUP_ROOT", cgroups),
                  patch.object(process_stop, "PROC", fixture.proc),
                  patch.object(os, "pidfd_open", side_effect=open_pidfd),
                  patch.object(signal, "pidfd_send_signal", side_effect=send_term)):
                process_stop.stop_slice(identity, 0, 0)
                self.assertTrue(process_stop.slice_has_ended(identity))
            self.assertEqual(signals, [(701, signal.SIGTERM)])

"""Check process stops with private process and scope records."""
from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.lib import process_start, process_stop


LEADER = process_stop.ProcessLifetime(701, "11")
UNIT = process_stop.ScopeUnit("builds-detached.slice", "detached-fixture.scope")


def write_process(root: Path, pid: int, start: int, *, cgroup: str = "", session: int = 701) -> None:
    directory = root / str(pid)
    directory.mkdir()
    fields = ["S", "1", str(session), str(session), *(["0"] * 15), str(start)]
    _ = (directory / "stat").write_text(f"{pid} (test process) " + " ".join(fields))
    _ = (directory / "cgroup").write_text(f"0::{cgroup}\n")


class StopFixture:
    def __init__(self, folder: Path) -> None:
        self.proc: Path = folder / "proc"
        self.proc.mkdir()
        self.cgroups: Path = folder / "cgroups"
        write_process(self.proc, LEADER.pid, int(LEADER.start))

    def scope(self, parent: str, *pids: int) -> tuple[str, Path]:
        relative = f"/{parent}/{UNIT.slice}/{UNIT.unit}"
        scope = self.cgroups / relative.lstrip("/")
        scope.mkdir(parents=True)
        _ = (scope / "cgroup.procs").write_text("".join(f"{pid}\n" for pid in pids))
        kill_file = scope / "cgroup.kill"
        _ = kill_file.write_text("0\n")
        return relative, kill_file

    def stop(self, scope: process_stop.ScopeUnit | process_stop.NoScopeUnit,
             number: int) -> list[tuple[int, int]]:
        pids: dict[int, int] = {}
        signals: list[tuple[int, int]] = []
        with ExitStack() as cleanup:
            def open_pidfd(pid: int) -> int:
                # Keep the pipe open to represent a process that has not exited.
                read, write = os.pipe()
                _ = cleanup.callback(os.close, write)
                pids[read] = pid
                return read

            def record_signal(descriptor: int, signum: int) -> None:
                signals.append((pids[descriptor], signum))

            with (patch.object(process_stop, "PROC", self.proc),
                  patch.object(process_stop, "CGROUP_ROOT", self.cgroups),
                  patch.object(os, "pidfd_open", side_effect=open_pidfd),
                  patch.object(signal, "pidfd_send_signal", side_effect=record_signal)):
                process_stop.stop_command_tree(LEADER, scope, number)
        return signals


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
        for member, expected_kill in ((702, "1\n"), (704, "0\n")):
            with self.subTest(member=member), TemporaryDirectory() as folder:
                fixture = StopFixture(Path(folder))
                relative, kill_file = fixture.scope("runs", member)
                write_process(fixture.proc, 702, 22, cgroup=relative)
                write_process(fixture.proc, 703, 33)
                signals = fixture.stop(UNIT, signal.SIGKILL)
                self.assertEqual(kill_file.read_text(), expected_kill)
                signaled = (701, 703) if member == 702 else (701, 702, 703)
                self.assertCountEqual(signals, [(pid, signal.SIGKILL) for pid in signaled])

    def test_without_a_scope_term_reaches_the_original_group(self) -> None:
        with TemporaryDirectory() as folder:
            fixture = StopFixture(Path(folder))
            write_process(fixture.proc, 702, 22)
            write_process(fixture.proc, 703, 33, session=703)
            self.assertCountEqual(fixture.stop(process_stop.NoScopeUnit(), signal.SIGTERM),
                                  [(701, signal.SIGTERM), (702, signal.SIGTERM)])

    def test_kill_write_error_falls_back_only_for_the_failed_scope(self) -> None:
        with TemporaryDirectory() as folder:
            fixture = StopFixture(Path(folder))
            failed, failed_kill = fixture.scope("failed", 702, 704)
            stopped, stopped_kill = fixture.scope("stopped", 703)
            write_process(fixture.proc, 702, 22, cgroup=failed)
            write_process(fixture.proc, 703, 33, cgroup=stopped)
            write_process(fixture.proc, 704, 44, cgroup=failed, session=704)
            write = os.write
            failed_file = failed_kill.stat()

            def write_kill(descriptor: int, data: bytes) -> int:
                if os.path.samestat(os.fstat(descriptor), failed_file):
                    raise PermissionError("fixture denies the kill write")
                return write(descriptor, data)

            with patch.object(os, "write", side_effect=write_kill):
                signals = fixture.stop(UNIT, signal.SIGKILL)
            self.assertEqual(failed_kill.read_text(), "0\n")
            self.assertEqual(stopped_kill.read_text(), "1\n")
            self.assertCountEqual(signals, [(pid, signal.SIGKILL) for pid in (701, 702, 704)])

    def test_term_reaches_scope_members_without_writing_cgroup_kill(self) -> None:
        with TemporaryDirectory() as folder:
            fixture = StopFixture(Path(folder))
            relative, kill_file = fixture.scope("runs", 702, 703)
            write_process(fixture.proc, 702, 22, cgroup=relative)
            write_process(fixture.proc, 703, 33, cgroup=relative, session=703)
            signals = fixture.stop(UNIT, signal.SIGTERM)
            self.assertEqual(kill_file.read_text(), "0\n")
            self.assertCountEqual(signals, [(pid, signal.SIGTERM) for pid in (701, 702, 703)])

    def test_term_reaches_a_child_that_joins_between_membership_reads(self) -> None:
        with TemporaryDirectory() as folder:
            fixture = StopFixture(Path(folder))
            relative, kill_file = fixture.scope("runs", 701, 702)
            _ = (fixture.proc / "701" / "cgroup").write_text(f"0::{relative}\n")
            write_process(fixture.proc, 702, 22, cgroup=relative, session=702)
            write_process(fixture.proc, 703, 33, cgroup=relative, session=703)
            read_start = process_start.proc_table_start

            def join_during_pin(pid: int, proc: Path) -> process_start.ProcessStart:
                if pid == 702:
                    _ = kill_file.with_name("cgroup.procs").write_text("701\n702\n703\n")
                return read_start(pid, proc)

            with patch.object(process_start, "proc_table_start", side_effect=join_during_pin):
                signals = fixture.stop(UNIT, signal.SIGTERM)
            self.assertCountEqual(signals, [(pid, signal.SIGTERM) for pid in (701, 702, 703)])

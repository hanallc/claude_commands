"""Check process stops with private process and scope records."""
from __future__ import annotations

from contextlib import ExitStack
import errno
import os
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import ANY, patch

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

    def test_receipt_failure_still_kills_a_scope_that_ignores_term(self) -> None:
        with TemporaryDirectory() as folder:
            root = Path(folder)
            fixture = StopFixture(root)
            cgroups = root / "cgroup"
            scope = cgroups / "builds.slice" / "fixture.scope"
            scope.mkdir(parents=True)
            relative = "/" + str(scope.relative_to(cgroups))
            _ = (fixture.proc / str(LEADER.pid) / "cgroup").write_text(f"0::{relative}\n")
            _ = (scope / "cgroup.procs").write_text(f"{LEADER.pid}\n")
            freeze = scope / "cgroup.freeze"
            _ = freeze.write_text("0\n")
            events = scope / "cgroup.events"
            _ = events.write_text("populated 1\nfrozen 0\n")
            kill = scope / "cgroup.kill"
            _ = kill.write_text("0\n")
            identity_stat = scope.stat()
            identity: process_stop.ScopeIdentity = {
                "path": relative, "unit": scope.name, "device": identity_stat.st_dev, "inode": identity_stat.st_ino,
            }
            now = 0.0
            failure = OSError("cannot save the TERM receipt")

            def receipt(count: int) -> None:
                self.assertEqual(freeze.read_text(), "1\n")
                self.assertEqual(count, 1)
                self.assertEqual(kill.read_text(), "0\n")
                raise failure

            def advance_clock(_descriptor: int, seconds: float) -> None:
                nonlocal now
                if freeze.read_text() == "1\n":
                    send.assert_not_called()
                    _ = events.write_text("populated 1\nfrozen 1\n")
                    return
                if kill.read_text() == "1\n":
                    self.assertGreaterEqual(now, 2)
                    _ = events.write_text("populated 0\nfrozen 0\n")
                now += seconds

            def open_pidfd(pid: int) -> int:
                self.assertEqual(pid, LEADER.pid)
                return os.open(os.devnull, os.O_RDONLY)

            with (patch.object(process_stop, "CGROUP_ROOT", cgroups),
                  patch.object(process_stop, "PROC", fixture.proc),
                  patch.object(os, "pidfd_open", side_effect=open_pidfd),
                  patch.object(signal, "pidfd_send_signal") as send,
                  patch.object(time, "monotonic", side_effect=lambda: now),
                  patch.object(process_stop, "_wait_scope_event", side_effect=advance_clock)):
                with self.assertRaises(OSError) as raised:
                    _ = process_stop.stop_scope(identity, 2, 2, on_term=receipt)
                self.assertIs(raised.exception, failure)
                send.assert_called_once_with(ANY, signal.SIGTERM)
                self.assertEqual(kill.read_text(), "1\n")
                self.assertTrue(process_stop.scope_has_ended(identity))

    def test_freeze_denial_thaws_every_scope_that_froze(self) -> None:
        with TemporaryDirectory() as folder:
            cgroups = Path(folder)
            scopes: list[process_stop.ScopeIdentity] = []
            controls: list[Path] = []
            for unit in ("first.scope", "second.scope", "denied.scope"):
                directory = cgroups / unit
                directory.mkdir()
                stat = directory.stat()
                scopes.append({"path": f"/{unit}", "unit": unit, "device": stat.st_dev, "inode": stat.st_ino})
                _ = (directory / "cgroup.events").write_text("populated 1\nfrozen 1\n")
                control = directory / "cgroup.freeze"
                _ = control.write_text("0\n")
                controls.append(control)
            write = os.write
            denied = controls[-1].stat()

            def write_control(descriptor: int, data: bytes) -> int:
                if os.path.samestat(os.fstat(descriptor), denied):
                    self.assertEqual([control.read_text() for control in controls[:2]], ["1\n", "1\n"])
                    raise PermissionError(errno.EACCES, "Permission denied", "cgroup.freeze")
                return write(descriptor, data)

            with (patch.object(process_stop, "CGROUP_ROOT", cgroups),
                  patch.object(os, "write", side_effect=write_control),
                  patch.object(signal, "pidfd_send_signal") as send,
                  self.assertRaisesRegex(process_stop.ScopeStopFailed,
                                         "denied.scope: cannot freeze scope with cgroup.freeze: "
                                         + r"\[Errno 13\] Permission denied: 'cgroup.freeze'")):
                _ = process_stop.stop_scopes(tuple(scopes), 2, 2)
            send.assert_not_called()
            self.assertEqual([control.read_text() for control in controls], ["0\n", "0\n", "0\n"])

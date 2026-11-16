"""App admission uses private state and stub programs."""
from __future__ import annotations

import os
import subprocess
import shutil
import signal
import time
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from typing import cast
from unittest import mock

from scripts.build_queue import app_gate, admission_client as client, queue_place
from scripts.build_queue import admission_protocol as wire, admission_records as records
from scripts.buildlog import store
from scripts.lint import memory_admit as gate
from scripts.build_queue.admission_test_support import AdmissionFixture, only_admitted


class AppGateTests(unittest.TestCase):
    def test_supervisor_admits_before_start_and_releases_after_exit(self) -> None:
        with AdmissionFixture() as fixture:
            scope = fixture.root / "scope"
            scope.mkdir()
            _ = (scope / "cgroup.procs").write_text(str(os.getpid()))
            _ = (scope / "memory.peak").write_text("7")
            _ = (scope / "memory.swap.peak").write_text("5")
            marker = fixture.root / "started"

            def admit(queued: queue_place.RefreshedRequest, output: client.WaitOutput) -> wire.Admitted:
                request = queued.refresh()
                self.assertFalse(marker.exists())
                self.assertIn("feature", request.identity.step)
                self.assertEqual(request.identity.name, "owner")
                output.status("waiting for memory: private queue reason")
                self.assertEqual(marker.with_suffix(".status").read_text().strip(), "waiting for memory: private queue reason")
                output.finished()
                return only_admitted(fixture.server.handle_request(request))

            def release(reservation: str, status: int, timing: wire.CleanupTiming) -> wire.Reply:
                self.assertTrue(marker.exists())
                self.assertEqual(status, 17)
                self.assertGreater(timing.silence_seconds, 0)
                return fixture.server.handle_request(wire.ReleaseRequest("release", reservation, status))[0]

            with mock.patch.object(queue_place, "request_place", side_effect=admit), \
                 mock.patch.object(queue_place, "release_place", side_effect=release):
                status = app_gate.supervise(Path(sys.executable), fixture.root, "feature", "owner", marker.with_suffix(".pid"),
                    [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('yes'); raise SystemExit(17)"], scope)
            self.assertEqual(status, 17)
            self.assertEqual(fixture.server.reservations, {})

    def test_refusal_starts_no_app(self) -> None:
        with AdmissionFixture() as fixture:
            with mock.patch.object(client, "wait_for_admission", return_value=wire.Refused("private refusal")), \
                 mock.patch.object(subprocess, "Popen") as process:
                status = app_gate.supervise(Path(sys.executable), fixture.root, "feature", "owner", fixture.root / "pid", [], fixture.root)
            self.assertEqual(status, 2)
            process.assert_not_called()

    def test_pid_file_failure_keeps_the_hold_until_the_scope_is_empty(self) -> None:
        with AdmissionFixture() as fixture:
            scope = fixture.root / "scope"
            scope.mkdir()
            pid_file = fixture.root / "pid"
            exited = [False]
            grant = only_admitted(fixture.server.handle_request(fixture.request(step="app")))

            def write(path: Path, text: str) -> None:
                if path == pid_file:
                    raise OSError("pid file unavailable")
                _ = path.write_text(text)

            def tick(seconds: float) -> None:
                self.assertGreater(seconds, 0)
                exited[0] = True

            def members(directory: Path) -> set[int]:
                self.assertEqual(directory, scope)
                return {os.getpid()} if exited[0] else {os.getpid(), 424243}

            def release(reservation: str, status: int, timing: wire.CleanupTiming) -> wire.Ok:
                self.assertEqual(reservation, grant.reservation)
                self.assertEqual(status, 1)
                self.assertGreater(timing.silence_seconds, 0)
                self.assertTrue(exited[0], "The hold ended while the app still used memory.")
                return wire.Ok()

            with (
                mock.patch.object(queue_place, "request_place", return_value=grant),
                mock.patch.object(queue_place, "release_place", side_effect=release),
                mock.patch.object(subprocess, "Popen", return_value=mock.Mock(pid=424243, wait=mock.Mock(return_value=0))),
                mock.patch.object(app_gate, "write_text_atomic", side_effect=write),
                mock.patch.object(gate, "scope_members", side_effect=members),
                mock.patch.object(os, "kill"),
                mock.patch.object(records, "process_start_time", return_value="123"),
                mock.patch.object(time, "sleep", side_effect=tick),
                self.assertRaisesRegex(OSError, "pid file unavailable"),
            ):
                _ = app_gate.supervise(Path(sys.executable), fixture.root, "feature", "owner", pid_file, [], scope)

    def test_periodic_peak_survives_scope_removal_before_service_cleanup(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="app")
            sidecar = fixture.scope(request.owner)
            scope = Path(sidecar.read_text())
            _ = (scope / "memory.peak").write_text("11")
            _ = (scope / "memory.swap.peak").write_text("3")
            request = replace(request, sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            calls = [0]

            def wait(timeout: float | None = None) -> int:
                calls[0] += 1
                if timeout is not None and calls[0] == 1:
                    raise subprocess.TimeoutExpired("fixture app", timeout or 1)
                shutil.rmtree(scope)
                raise RuntimeError("supervisor stopped")

            with (
                mock.patch.object(queue_place, "request_place", return_value=grant),
                mock.patch.object(queue_place, "release_place", return_value=wire.Ok()),
                mock.patch.object(subprocess, "Popen", return_value=mock.Mock(pid=424243, wait=wait)),
                mock.patch.object(records, "process_start_time", return_value="123"),
                mock.patch.object(gate, "scope_members", return_value=set()),
                self.assertRaisesRegex(RuntimeError, "supervisor stopped"),
            ):
                _ = app_gate.supervise(Path(sys.executable), fixture.root, "feature", "owner", fixture.root / "pid", [], scope)
            self.assertFalse(scope.exists())
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 1)), [wire.Ok()])
            history = list(store.locked_rows(fixture.state_dir / "anon_peaks.jsonl"))
            self.assertTrue(history, "The scope peak was lost before service cleanup.")
            self.assertEqual(history[-1]["anon_peak_bytes"], 14)

    def test_pid_publication_failure_stops_the_app_before_waiting(self) -> None:
        with AdmissionFixture() as fixture:
            scope = fixture.root / "scope"
            scope.mkdir()
            _ = (scope / "cgroup.procs").write_text(str(os.getpid()))
            pid_file = fixture.root / "app.pid"
            grant = only_admitted(fixture.server.handle_request(fixture.request(step="app")))
            killed = mock.Mock()
            waited = mock.Mock()
            process = mock.Mock(pid=424243, kill=killed, wait=waited)

            def publish(path: Path, text: str) -> None:
                if path == pid_file:
                    raise OSError("pid publication failed")
                _ = path.write_text(text)

            def finish(timeout: float) -> int:
                self.assertGreater(timeout, 0)
                killed.assert_called_once()
                return 0

            waited.side_effect = finish
            with (
                mock.patch.object(queue_place, "request_place", return_value=grant),
                mock.patch.object(queue_place, "release_place", return_value=wire.Ok()) as release,
                mock.patch.object(subprocess, "Popen", return_value=process),
                mock.patch.object(records, "process_start_time", return_value="123"),
                mock.patch.object(app_gate, "write_text_atomic", side_effect=publish),
                self.assertRaisesRegex(OSError, "pid publication failed"),
            ):
                _ = app_gate.supervise(Path(sys.executable), fixture.root, "feature", "owner", pid_file, [], scope)
            waited.assert_called_once()
            release.assert_called_once_with(grant.reservation, 1, grant.cleanup_timing)
            self.assertIn("pid publication failed", pid_file.with_suffix(".status").read_text())

    def test_startup_failure_retains_cleanup_before_the_slice_starts(self) -> None:
        for failure in ("slice", "exec", "scope"):
            with self.subTest(failure=failure), AdmissionFixture() as fixture:
                pid_file = fixture.root / "app.pid"
                records_dir = fixture.state_dir / "app-scopes"

                def start_slice(name: str) -> None:
                    sidecars = list(records_dir.glob("*.cgroup"))
                    self.assertEqual(len(sidecars), 1, "The slice starts without a cleanup record.")
                    scope = Path(sidecars[0].read_text().strip())
                    self.assertEqual(scope.parent.name, name)
                    self.assertEqual(pid_file.with_suffix(".cgroup").read_text().strip(), str(scope))
                    fixture.restart()
                    _ = fixture.run_pass()
                    self.assertTrue(sidecars[0].exists(), "Cleanup must retain a live launcher's record.")
                    if failure == "slice":
                        raise OSError("slice failed")

                with (
                    mock.patch.object(gate, "builds_slice_cgroup", return_value=fixture.root / "builds.slice"),
                    mock.patch.object(app_gate, "start_peak_slice", side_effect=start_slice),
                    mock.patch.object(os, "execvp", side_effect=OSError(failure + " failed")),
                    self.assertRaisesRegex(OSError, failure + " failed"),
                ):
                    _ = app_gate.main(["--binary", sys.executable, "--repo", str(fixture.root),
                        "--purpose", "startup failure", "--pid-file", str(pid_file)])
                sidecars = list(records_dir.glob("*.cgroup"))
                self.assertEqual(len(sidecars), 1)
                scope = Path(sidecars[0].read_text().strip())
                with (
                    mock.patch.object(records, "process_start_time", return_value=None),
                    mock.patch.object(subprocess, "run") as stop,
                ):
                    fixture.restart()
                    _ = fixture.run_pass()
                stop.assert_called_once_with(["systemctl", "--user", "--no-block", "stop", scope.parent.name],
                    check=True, capture_output=True, text=True)
                self.assertFalse(sidecars[0].exists())
                self.assertFalse(sidecars[0].with_suffix(".owner").exists())

    def test_cancel_before_scope_creation_stops_the_saved_launcher(self) -> None:
        with AdmissionFixture() as fixture:
            pid_file = fixture.root / "app.pid"

            def cancel(name: str) -> None:
                self.assertTrue(name.endswith(".slice"))
                scope_file = pid_file.with_suffix(".cgroup")
                scope = Path(scope_file.read_text().strip())
                self.assertFalse(scope.exists())
                with (
                    mock.patch.object(records, "process_start_time", return_value="123"),
                    mock.patch.object(os, "kill") as terminate,
                    mock.patch.object(time, "sleep"),
                ):
                    app_gate.stop_app_scope(scope_file, 2)
                self.assertEqual(terminate.call_args_list,
                    [mock.call(os.getpid(), signal.SIGTERM), mock.call(os.getpid(), signal.SIGKILL)])
                raise OSError("launcher cancelled")

            with (
                mock.patch.object(gate, "builds_slice_cgroup", return_value=fixture.root / "builds.slice"),
                mock.patch.object(records, "process_start_time", return_value="123"),
                mock.patch.object(app_gate, "start_peak_slice", side_effect=cancel),
                mock.patch.object(os, "execvp") as execute,
                self.assertRaisesRegex(OSError, "launcher cancelled"),
            ):
                _ = app_gate.main(["--binary", sys.executable, "--repo", str(fixture.root),
                    "--purpose", "cancel startup", "--pid-file", str(pid_file)])
            execute.assert_not_called()

    def test_stop_reads_a_scope_published_during_the_launchers_grace(self) -> None:
        with AdmissionFixture() as fixture:
            scope_file = fixture.root / "app.cgroup"
            _ = scope_file.with_suffix(".owner").write_text("424242 123")
            scope = fixture.root / "app.scope"

            def publish(seconds: float) -> None:
                self.assertEqual(seconds, 2)
                scope.mkdir()
                _ = (scope / "cgroup.kill").write_text("0")
                _ = scope_file.write_text(str(scope))

            with (
                mock.patch.object(records, "process_start_time", return_value="123"),
                mock.patch.object(os, "kill") as terminate,
                mock.patch.object(time, "sleep", side_effect=publish),
            ):
                app_gate.stop_app_scope(scope_file, 2)
            self.assertEqual(terminate.call_args_list,
                [mock.call(424242, signal.SIGTERM), mock.call(424242, signal.SIGKILL)])
            self.assertEqual((scope / "cgroup.kill").read_text(), "1")

    def test_scope_stop_does_not_signal_a_reused_launcher_pid(self) -> None:
        with AdmissionFixture() as fixture:
            scope_file = fixture.root / "app.cgroup"
            _ = scope_file.with_suffix(".owner").write_text("424242 123")
            with (
                mock.patch.object(records, "process_start_time", return_value="124"),
                mock.patch.object(os, "kill") as terminate,
                mock.patch.object(time, "sleep"),
            ):
                app_gate.stop_app_scope(scope_file, 2)
            terminate.assert_not_called()

    def test_scope_stop_does_not_signal_an_exited_fallback_launcher(self) -> None:
        with AdmissionFixture() as fixture:
            signalled = mock.Mock()
            process = cast(subprocess.Popen[bytes], mock.Mock(
                pid=424242, poll=mock.Mock(return_value=0), send_signal=signalled))
            with mock.patch.object(time, "sleep"):
                app_gate.stop_app_scope(fixture.root / "app.cgroup", 2, fallback_launcher=process)
            signalled.assert_not_called()

    def test_scope_stop_uses_the_owned_child_without_any_metadata(self) -> None:
        with AdmissionFixture() as fixture:
            with subprocess.Popen([sys.executable, "-c",
                "import time; time.sleep(300)"], start_new_session=True) as process:
                try:
                    app_gate.stop_app_scope(fixture.root / "app.cgroup", 0, fallback_launcher=process)
                    self.assertIn(process.wait(timeout=2), (-signal.SIGTERM, -signal.SIGKILL))
                finally:
                    if process.poll() is None:
                        process.kill()
                    _ = process.wait(timeout=2)

    def test_scope_stop_reaches_a_descendant_in_a_new_process_group(self) -> None:
        with AdmissionFixture() as fixture:
            scope = fixture.root / "builds-appfixture.slice/app.scope"
            scope.mkdir(parents=True)
            scope_file = fixture.root / "app.cgroup"
            _ = scope_file.write_text(str(scope))
            _ = (scope / "cgroup.kill").write_text("0")
            process: subprocess.Popen[bytes] = subprocess.Popen([sys.executable, "-c",
                "import os,time; print(os.getpgrp(),flush=True); time.sleep(300)"],
                start_new_session=True, stdout=subprocess.PIPE)
            with process as descendant:
                try:
                    assert descendant.stdout is not None
                    group_line = cast(object, descendant.stdout.readline())
                    assert isinstance(group_line, bytes)
                    self.assertEqual(int(group_line), descendant.pid)
                    self.assertNotEqual(os.getpgrp(), descendant.pid)
                    _ = (scope / "cgroup.procs").write_text(str(descendant.pid))
                    app_gate.stop_app_scope(scope_file, 0)
                    self.assertEqual(descendant.wait(timeout=2), -signal.SIGTERM)
                    self.assertEqual((scope / "cgroup.kill").read_text(), "1")
                    self.assertTrue(scope_file.exists(), "The service must retain the final peak cleanup record.")
                finally:
                    if descendant.poll() is None:
                        descendant.kill()
                    _ = descendant.wait(timeout=2)

    def test_scope_stop_terms_all_members_before_the_grace_and_kills_new_members(self) -> None:
        with AdmissionFixture() as fixture:
            scope = fixture.root / "scope"
            scope.mkdir()
            scope_file = fixture.root / "app.cgroup"
            _ = scope_file.write_text(str(scope))
            _ = (scope / "cgroup.procs").write_text("424243\n424244")
            _ = (scope / "cgroup.kill").write_text("0")
            with mock.patch.object(os, "kill") as terminate:
                def grace(seconds: float) -> None:
                    self.assertEqual(seconds, 2)
                    self.assertCountEqual(terminate.call_args_list,
                        [mock.call(424243, signal.SIGTERM), mock.call(424244, signal.SIGTERM)])
                    _ = (scope / "cgroup.procs").write_text("424245")
                    self.assertEqual((scope / "cgroup.kill").read_text(), "0")

                with mock.patch.object(time, "sleep", side_effect=grace):
                    self.assertEqual(app_gate.main(["stop", "--scope-file", str(scope_file), "--grace", "2"]), 0)
            self.assertEqual((scope / "cgroup.kill").read_text(), "1")

    def test_command_enters_its_own_scope(self) -> None:
        with AdmissionFixture() as fixture:
            with (mock.patch.object(os, "execvp", side_effect=RuntimeError("exec")) as execute,
                  mock.patch.object(subprocess, "run") as create):
                with self.assertRaisesRegex(RuntimeError, "exec"):
                    _ = app_gate.main(["--binary", sys.executable, "--repo", str(fixture.root),
                        "--purpose", "feature", "--owner", "owner", "--pid-file", str(fixture.root / "pid")])
            command = cast(list[str], execute.call_args.args[1])
            self.assertIn("--scope", command)
            self.assertIn("--in-scope", command)
            peak_slice = next(argument for argument in command if argument.startswith("--slice="))
            self.assertRegex(peak_slice, r"^--slice=builds-app[0-9a-f]{32}\.slice$")
            create.assert_called_once_with(["busctl", "--user", "call", "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "StartTransientUnit",
                "ssa(sv)a(sa(sv))", peak_slice.removeprefix("--slice="), "fail", "2", "MemoryAccounting", "b", "true",
                "StopWhenUnneeded", "b", "false", "0"], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    _ = unittest.main()

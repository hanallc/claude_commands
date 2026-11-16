"""App admission uses private state and stub programs."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import os
import json
import subprocess
import shutil
import select
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
from scripts.build_queue import admission_service as service
from scripts.build_queue.queue_fixture import write_process, write_scope
from scripts.buildlog import store
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import write_config_fixture
from scripts.build_queue.admission_test_support import AdmissionFixture, only_admitted


class AppGateTests(unittest.TestCase):
    def test_service_counts_adopted_processes_outside_the_holder_tree_once(self) -> None:
        for start, adopted_bytes in (("100", 6 * records.GIB), ("reused", 0)):
            with self.subTest(start=start), AdmissionFixture() as fixture:
                holder = fixture.proc.spawn(used=records.GIB)
                sidecar = fixture.root / "app.cgroup"
                _ = sidecar.write_text("")
                _ = sidecar.with_suffix(".adopt").write_text(json.dumps({"owner_pid": holder.pid,
                    "owner_start": holder.start_time, "processes": {"700010": "100"}}))
                request = replace(fixture.request(owner=holder, need=records.GIB, step="app"), sidecar=str(sidecar))
                _ = only_admitted(fixture.server.handle_request(request))
                write_process(fixture.proc.path, 700010, 4 * records.GIB, start=start)
                write_process(fixture.proc.path, 700011, 2 * records.GIB, parent=700010, start="200")
                write_process(fixture.proc.path, 700012, 4 * records.GIB, start="300")
                scope = fixture.root / "builds.slice/foreign.scope"
                write_scope(scope, members=(700010, 700011, 700012),
                    current=10 * records.GIB, anonymous=10 * records.GIB)
                _ = fixture.run_pass()
                evidence = fixture.server.system_evidence
                assert isinstance(evidence, service.SystemEvidence)
                memory = evidence.reservations[request.episode].memory
                assert isinstance(memory, records.MeasuredMemory)
                self.assertEqual(memory.bytes, records.GIB + adopted_bytes)
                self.assertEqual(memory.anonymous, records.AnonymousRunUse(records.GIB + adopted_bytes))
                inputs = fixture.server.last_inputs
                assert isinstance(inputs, records.QueueInputs)
                held = next(row for row in inputs.running if row.pid == holder.pid)
                assert isinstance(held.measured, records.MeasuredMemory)
                self.assertEqual(held.measured.bytes, records.GIB + adopted_bytes)
                remaining = next(row.measured for row in inputs.running
                    if isinstance(row.measured, records.UnownedSliceMemory) and row.measured.cgroup == str(scope))
                self.assertEqual(remaining.bytes, 10 * records.GIB - adopted_bytes)
                self.assertEqual(remaining.anonymous, records.AnonymousRunUse(10 * records.GIB - adopted_bytes))

    def test_silent_admission_ends_at_the_deadline_or_cancellation(self) -> None:
        from scripts.build_queue.test_admission_service import FixedSystemReader

        holder = """
import json
from pathlib import Path
import sys
from unittest import mock
from scripts.build_queue import app_gate, admission_client as client, admission_protocol as wire, queue_place
from scripts.build_queue.admission_test_support import FakeClock, Exchange, ScriptedTransport, ScriptSocket, TimedChunk

outcome, folder = sys.argv[1:]
directory = Path(folder)
clock = FakeClock()
transport = ScriptedTransport(clock, Exchange())
reads = []
original_request = queue_place.request_place

def send(connection, data):
    request = wire.parse_request(data)
    if isinstance(request, wire.ReleaseRequest):
        raise AssertionError('synchronous release would wait on the stalled service')
    transport.requests.append(request)
    sys.stdout.write(data.decode())
    sys.stdout.flush()
    reply = sys.stdin.readline().encode()
    if outcome == 'partial':
        split = len(reply) // 2
        events = [TimedChunk(0.2, reply[:split]), reply[split:]]
    elif outcome in ('saved-silent', 'cancel'):
        events = [TimeoutError() for _ in range(10)]
    elif outcome == 'late':
        events = [TimedChunk(0.2, reply)]
    else:
        events = [reply]
    connection.exchange.events.extend(events)

original_recv = ScriptSocket.recv

def receive(connection, size):
    reads.append(clock.monotonic())
    if outcome == 'cancel':
        (directory / 'cancel').write_text('hook cancelled')
    return original_recv(connection, size)

def request(*args, **kwargs):
    reply = original_request(*args, **kwargs)
    if outcome == 'returned-late':
        clock.advance(0.2)
    return reply

with (mock.patch.object(client, 'CLOCK', clock),
      mock.patch.object(app_gate.time, 'monotonic', side_effect=clock.monotonic),
      mock.patch.object(client.socket, 'socket', side_effect=transport.socket),
      mock.patch.object(ScriptSocket, 'sendall', new=send),
      mock.patch.object(ScriptSocket, 'recv', new=receive),
      mock.patch.object(queue_place, 'request_place', side_effect=request)):
    result = app_gate.adopt(app_gate.AdoptLaunch(directory / 'hana', directory, 'feature', 1), directory, 0.2, 2)
print(json.dumps({'result': result, 'elapsed': clock.monotonic(), 'reads': len(reads)}), flush=True)
raise SystemExit(result)
"""
        for outcome in ("partial", "saved-silent", "cancel", "late", "returned-late"):
            with self.subTest(outcome=outcome), AdmissionFixture() as fixture:
                _ = write_config_fixture(fixture.root / "build_admission.conf", "brp_adopt_check_seconds=0.05\n")
                directory = fixture.state_dir / "brp-holds" / outcome
                if outcome == "cancel":
                    _ = fixture.occupy_host()
                    fixture.free(0)
                began = time.monotonic()
                with subprocess.Popen([sys.executable, "-c", holder, outcome, str(directory)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
                    try:
                        assert child.stdout is not None and child.stdin is not None
                        self.assertTrue(select.select([child.stdout], [], [], 3)[0], "The holder sent no request.")
                        request_line = cast(object, child.stdout.readline())
                        assert isinstance(request_line, str)
                        request = wire.parse_request(request_line)
                        assert isinstance(request, wire.AdmitRequest)
                        fixture.proc.register(request.owner)
                        replies = fixture.server.handle_request(request)
                        reply = replies[-1]
                        self.assertIsInstance(reply, wire.Wait if outcome == "cancel" else wire.Admitted)
                        _ = fixture.run_pass()
                        evidence = fixture.server.system_evidence
                        assert isinstance(evidence, service.SystemEvidence)
                        reader = FixedSystemReader(evidence)
                        fixture.server.system_reader = reader
                        output, errors = child.communicate(wire.encode_reply(reply), timeout=3)
                        self.assertEqual(child.returncode, 2, errors)
                    finally:
                        if child.poll() is None:
                            child.kill()
                        _ = child.wait(timeout=3)
                self.assertLess(time.monotonic() - began, 3)
                report = app_gate.object_value(cast(object, json.loads(output)))
                elapsed = report["elapsed"]
                assert isinstance(elapsed, (int, float))
                self.assertLessEqual(elapsed, 0.2)
                self.assertEqual(report["reads"], 4 if outcome == "saved-silent" else 1)
                self.assertIn("launch cancelled" if outcome == "cancel" else "launch wait limit reached",
                    (directory / "refused").read_text())
                for name in ("app.cgroup", "app.adopt", "holder.owner", "status", "refused"):
                    self.assertTrue((directory / name).exists(), name)
                self.assertFalse((directory / "released").exists())
                _ = fixture.run_pass()
                if outcome == "cancel":
                    self.assertIn(request.episode, fixture.server.waiters)
                else:
                    self.assertIn(request.episode, fixture.server.reservations)
                    self.assertTrue((directory / "app.cgroup").exists())
                shutil.rmtree(fixture.proc.path / str(request.owner.pid))
                reader.latest = service.read_system(reader.plans[-1], fixture.readings, fixture.proc, fixture.clock)
                with fixture.loop_turn():
                    fixture.server.run_once(0)
                _ = fixture.run_pass()
                self.assertNotIn(request.episode, fixture.server.waiters)
                self.assertNotIn(request.episode, fixture.server.reservations)
                _ = fixture.run_pass()
                self.assertFalse(directory.exists(), "The service left the dead holder's files.")

    def test_service_retains_refusal_files_until_the_hook_and_holder_exit(self) -> None:
        with AdmissionFixture() as fixture:
            holder = fixture.fixture_owner(700020, "holder")
            hook = fixture.fixture_owner(700021, "hook")
            directory = fixture.state_dir / "brp-holds" / "cancelled"
            directory.mkdir(parents=True)
            for name in ("app.cgroup", "app.adopt", "app.owner", "status", "refused", "granted", "released",
                         "cancel", "handoff.json", "holder.log"):
                _ = (directory / name).write_text("fixture")
            _ = (directory / "holder.owner").write_text(f"{holder.pid} {holder.start_time}")
            _ = (directory / "hook.owner").write_text(f"{hook.pid} {hook.start_time}")
            _ = fixture.run_pass()
            self.assertTrue((directory / "refused").exists())
            fixture.proc.fixture_owners[holder.pid] = "reused"
            _ = fixture.run_pass()
            self.assertTrue((directory / "refused").exists())
            fixture.proc.fixture_owners[hook.pid] = "reused"
            _ = fixture.run_pass()
            self.assertFalse(directory.exists())

    def test_adopt_holds_instances_and_descendants_until_their_identities_end(self) -> None:
        for instances, aggregate_peak, instance_peak in ((1, 12, 12), (3, 18 * records.GIB, 6 * records.GIB)):
            with self.subTest(instances=instances), AdmissionFixture() as fixture:
                fixture.clock.epoch = datetime.now(UTC).timestamp()
                directory = fixture.root / "adopt"
                directory.mkdir()
                launch = app_gate.AdoptLaunch(fixture.root / "hana", fixture.root, "feature", instances)
                now = [0.0]
                trees = [
                    {101: gate.ProcessIdentity(1, "a"), 102: gate.ProcessIdentity(101, "b")},
                    {102: gate.ProcessIdentity(1, "b"), 103: gate.ProcessIdentity(102, "c")},
                    {103: gate.ProcessIdentity(1, "c")},
                    {101: gate.ProcessIdentity(1, "reused")},
                ]
                samples: list[set[int]] = []

                def request(queued: queue_place.RefreshedRequest, output: client.WaitOutput, *,
                            before_check: Callable[[], None]) -> wire.Admitted:
                    self.assertIsInstance(before_check, object)
                    row = queued.refresh()
                    baseline = gate.app_profile(launch.binary, launch.repository,
                        datetime.now(UTC), store.root())
                    self.assertEqual(row.profile.peak.bytes, baseline.peak.bytes * instances)
                    self.assertEqual(row.owner.pid, os.getpid())
                    self.assertFalse((directory / "granted").exists())
                    output.status("private grant")
                    _ = (directory / "handoff.json").write_text(json.dumps({"owners": [{"pid": 101, "start_time": "a"}]}))
                    request = replace(fixture.request(step="app"), sidecar=str(directory / "app.cgroup"), profile=row.profile)
                    grant = only_admitted(fixture.server.handle_request(request))
                    samples.clear()
                    return grant

                def memory(members: set[int], proc: Path) -> records.MeasuredMemory:
                    self.assertEqual(proc, fixture.root / "proc")
                    samples.append(members.copy())
                    return records.MeasuredMemory(aggregate_peak - 5, swap_bytes=5) if members else records.MeasuredMemory(0)

                def tick(seconds: float) -> None:
                    self.assertTrue(fixture.server.reservations)
                    self.assertTrue((directory / "granted").exists())
                    now[0] += seconds
                    _ = trees.pop(0)

                def processes(proc: Path) -> dict[int, gate.ProcessIdentity]:
                    self.assertEqual(proc, fixture.root / "proc")
                    return trees[0]

                def release(reservation: str, status: int, timing: wire.CleanupTiming) -> wire.Ok:
                    self.assertEqual(samples, [{101, 102}, {102, 103}, {103}, set()])
                    self.assertGreater(timing.silence_seconds, 0)
                    self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("release", reservation, status)), [wire.Ok()])
                    return wire.Ok()

                with (mock.patch.object(queue_place, "request_place", side_effect=request),
                      mock.patch.object(queue_place, "release_place", side_effect=release),
                      mock.patch.object(records, "process_start_time", return_value="owner"),
                      mock.patch.object(gate, "process_table", side_effect=processes),
                      mock.patch.object(gate, "process_memory", side_effect=memory),
                      mock.patch.object(time, "sleep", side_effect=tick),
                      mock.patch.object(time, "monotonic", side_effect=lambda: now[0])):
                    self.assertEqual(app_gate.adopt(launch, directory, 5, 2, proc=fixture.root / "proc"), 0)
                self.assertEqual(fixture.server.reservations, {})
                self.assertEqual(list(store.locked_rows(fixture.state_dir / "anon_peaks.jsonl"))[-1]["anon_peak_bytes"], instance_peak)
                refresh = app_gate.app_request(launch.binary, launch.repository, launch.purpose, "", directory / "app.cgroup",
                    wire.Owner(os.getpid(), "owner"), "next-launch", time.time(), (str(launch.binary),), instances)
                self.assertEqual(refresh().profile.peak.bytes, aggregate_peak)
                self.assertTrue((directory / "released").exists())

    def test_adopt_refusal_timeout_and_failed_or_missing_handoff_release(self) -> None:
        for outcome in ("refused", "unavailable", "wait-limit", "no-handoff", "failed-handoff", "already-ended",
                        "late-grant", "deadline-grant"):
            with self.subTest(outcome=outcome), AdmissionFixture() as fixture:
                directory = fixture.root / "adopt"
                directory.mkdir()
                now = [0.0]
                granted: list[wire.Admitted] = []

                def request(queued: queue_place.RefreshedRequest, output: client.WaitOutput, *,
                            before_check: Callable[[], None]) -> wire.Admitted | wire.Refused:
                    self.assertTrue(callable(before_check))
                    _ = queued
                    if outcome == "refused":
                        return wire.Refused("fixture policy refuses this app")
                    if outcome == "unavailable":
                        raise wire.AdmissionUnavailable("fixture service is unavailable")
                    if outcome == "wait-limit":
                        output.last_position = 3
                        output.last_reasons = ("need exceeds room",)
                        now[0] = 6
                        before_check()
                        self.fail("expired wait continued")
                    grant = only_admitted(fixture.server.handle_request(fixture.request(step="app")))
                    granted.append(grant)
                    if outcome == "failed-handoff":
                        _ = (directory / "handoff.json").write_text('{"error":"launch failed"}')
                    if outcome == "already-ended":
                        _ = (directory / "handoff.json").write_text('{"owners":[]}')
                    if outcome == "late-grant":
                        _ = (directory / "cancel").write_text("hook deadline")
                    if outcome == "deadline-grant":
                        now[0] = 5
                    return grant

                def tick(seconds: float) -> None:
                    now[0] += seconds

                def release(reservation: str, status: int, timing: wire.CleanupTiming) -> wire.Ok:
                    self.assertGreater(timing.silence_seconds, 0)
                    self.assertTrue((directory / "app.cgroup").exists())
                    self.assertTrue((directory / "app.adopt").exists())
                    self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("release", reservation, status)), [wire.Ok()])
                    return wire.Ok()

                with (mock.patch.object(queue_place, "request_place", side_effect=request),
                      mock.patch.object(queue_place, "release_place", side_effect=release) as released,
                      mock.patch.object(records, "process_start_time", return_value="owner"),
                      mock.patch.object(time, "sleep", side_effect=tick),
                      mock.patch.object(time, "monotonic", side_effect=lambda: now[0])):
                    result = app_gate.adopt(app_gate.AdoptLaunch(fixture.root / "hana", fixture.root, "feature", 1),
                                            directory, 5, 2)
                self.assertEqual(result, 0 if outcome == "already-ended" else 2)
                owner_exit = outcome in ("wait-limit", "no-handoff", "late-grant", "deadline-grant")
                self.assertEqual(released.call_count, 0 if owner_exit else len(granted))
                self.assertEqual(bool(fixture.server.reservations), owner_exit and bool(granted))
                retained = owner_exit or not granted
                self.assertEqual((directory / "app.cgroup").exists(), retained)
                self.assertEqual((directory / "app.adopt").exists(), retained)
                if outcome == "wait-limit":
                    self.assertIn("queue place 3: need exceeds room", (directory / "refused").read_text())

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

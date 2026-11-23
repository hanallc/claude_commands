"""The client's three failure outcomes keep episode identity and time bounds."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import errno
import fcntl
from functools import partial
import io
import json
import os
import re
import resource
import select
from selectors import DefaultSelector, EVENT_READ, EVENT_WRITE, SelectorKey
import shlex
import socket
import subprocess
import time
import threading
from pathlib import Path
import sys
import unittest
from unittest import mock

from scripts.build_queue import execution_identity
from scripts.accounts import outage_alerts
from scripts.message import send_result
from scripts.message import roster
from scripts.message.session_send import session_send_argv
from scripts.message.session_send_test_support import forbid_real_sender
from scripts.message.codex_test_support import FakeDaemon, receive_frame, reply, write_codex_account
from scripts.lib.socket_test_support import short_socket_directory
from scripts.lib.test_hang_guard import LauncherHangGuard
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_worker, fixture_runtime_id, isolated_staff_file
from typing import cast, override
from scripts.lib import conf
from scripts.build_queue import admission_client as client, admission_protocol as wire
from scripts.build_queue import queue_place as shell_place
from scripts.build_targets import target_config
from scripts.build_hold.owning_session import NoOwningSession
from scripts.mac_test import mac_test, claim_state
from scripts.lib import process_start as process_identity
from scripts.build_queue import admission_records as records, admission_service as service, step_estimator
from scripts.build_queue.admission_test_support import admitted_reply, wait_reply
from scripts.build_queue.admission_test_support import (
    AdmissionFixture, Exchange, GIB, InjectedCrash, ScriptedSelector, ScriptedTransport, ScriptSocket, TimedChunk, only_admitted, reply_bytes,
)
from scripts.build_queue.queue_fixture import QueueFixture, write_memory_config
from scripts.build_queue.test_snapshot_codec import service_snapshot
from scripts.lint.memory_test_support import commit_config_fixture, config_environment, write_config_fixture, write_pressure_reading

WAIT = wait_reply(records.NeedsMemory(), 1, "head", "free memory")
EXPIRED = wait_reply(records.EstimateExpired(1), 1, "head", "estimate expired")
GRANTED = admitted_reply("stem", GIB, "new-source", "stem.max", "stem.wave")


class PrivateSocketSelector(DefaultSelector):
    def __init__(self, fixture: AdmissionFixture, on_timeout: Callable[[], None] = lambda: None,
                 *, before_read: Callable[[], None] = lambda: None,
                 hide_read: Callable[[SelectorKey], bool] = lambda _key: False) -> None:
        super().__init__()
        self.fixture: AdmissionFixture = fixture
        self.on_timeout: Callable[[], None] = on_timeout
        self.before_read: Callable[[], None] = before_read
        self.hide_read: Callable[[SelectorKey], bool] = hide_read

    @override
    def select(self, timeout: float | None = None) -> list[tuple[SelectorKey, int]]:
        self.before_read()
        for _ in range(4):
            self.fixture.server.run_once(0)
        ready = [(key, mask) for key, mask in super().select(0)
                 if mask != EVENT_READ or not self.hide_read(key)]
        if not ready and timeout is not None and timeout > 0:
            self.fixture.clock.advance(timeout)
            self.on_timeout()
        return ready


def await_private_socket(fixture: AdmissionFixture, reader: socket.socket, timeout: float) -> None:
    with PrivateSocketSelector(fixture) as selector:
        _ = selector.register(reader, EVENT_READ)
        _ = selector.select(timeout)


def setUpModule() -> None:
    forbid_real_sender()


class ClientTests(unittest.TestCase):
    def test_stale_outer_markers_do_not_refuse_admission(self) -> None:
        for case in ("other-queue", "dead-holder", "released", "reused-pid"):
            with self.subTest(case=case), AdmissionFixture() as fixture:
                pid = os.getppid()
                start = records.process_start_time(pid)
                assert start is not None
                owner = fixture.proc.spawn() if case == "dead-holder" else wire.Owner(pid, start)
                outer = fixture.request(owner=owner)
                with fixture.loop_turn():
                    grant = only_admitted(fixture.server.handle_request(outer))
                directory = fixture.state_dir
                if case == "other-queue":
                    directory = fixture.root / "other-admission"
                elif case == "dead-holder":
                    fixture.proc.kill(outer.owner)
                elif case == "released":
                    fixture.release(grant)
                marker = f"{grant.reservation}\t{outer.owner.pid}\t{directory.resolve()}"
                nested = fixture.request()
                start = "reused" if case == "reused-pid" else records.process_start_time(outer.owner.pid)
                with mock.patch.dict(os.environ, {"BUILDLOG_RESERVED_STEP": marker}), \
                        mock.patch.object(records, "process_start_time", return_value=start), \
                        mock.patch.object(client, "_acquire", return_value=wire.Refused("fixture admission")) as acquire, \
                        redirect_stderr(io.StringIO()) as errors:
                    reply = client.admit(nested, lambda _wait: None, lambda: nested)
                self.assertEqual(reply, wire.Refused("fixture admission"))
                acquire.assert_called_once()
                self.assertEqual(errors.getvalue(), "")

    def test_nested_reserved_step_refuses_before_it_can_wait(self) -> None:
        with AdmissionFixture() as fixture:
            pid = os.getppid()
            start = records.process_start_time(pid)
            assert start is not None
            outer = fixture.request(owner=wire.Owner(pid, start))
            with fixture.loop_turn():
                grant = only_admitted(fixture.server.handle_request(outer))
            fixture.hold_starts()
            marker = f"{grant.reservation}\t{outer.owner.pid}\t{fixture.state_dir.resolve()}"
            nested = fixture.request()
            def waiting(_wait: wire.Wait) -> None:
                self.fail("the nested step waited for its outer reservation")
            with mock.patch.dict(os.environ, {"BUILDLOG_RESERVED_STEP": marker}), fixture.serving(), \
                    redirect_stderr(io.StringIO()) as errors, \
                    mock.patch.object(client, "_connect", side_effect=AssertionError("nested admission opened a socket")) as connect:
                reply = client.admit(nested, waiting, lambda: nested)
            self.assertIsInstance(reply, wire.Refused)
            self.assertEqual(errors.getvalue().splitlines(), [
                f"verify step cannot run inside reserved step {grant.reservation} (pid {outer.owner.pid}); "
                + "run it outside the outer step"])
            connect.assert_not_called()

    def test_live_reservation_of_a_non_ancestor_does_not_skip_admission(self) -> None:
        with AdmissionFixture() as fixture:
            outer = fixture.request()
            with fixture.loop_turn():
                grant = only_admitted(fixture.server.handle_request(outer))
            marker = f"{grant.reservation}\t{outer.owner.pid}\t{fixture.state_dir}\t{fixture.socket_path}"
            nested = fixture.request()
            with (mock.patch.dict(os.environ, {"BUILDLOG_RESERVED_STEP": marker}),
                  mock.patch.object(client, "_acquire", return_value=wire.Refused("fixture admission")) as acquire):
                self.assertIsInstance(client.outer_reservation(match_cpu_pool=True), client.NoOuterReservation)
                self.assertEqual(client.admit(nested, lambda _wait: None, lambda: nested), wire.Refused("fixture admission"))
            acquire.assert_called_once()

    def test_reservation_pending_release_does_not_skip_admission(self) -> None:
        with AdmissionFixture() as fixture:
            pid = os.getppid()
            start = records.process_start_time(pid)
            assert start is not None
            outer = fixture.request(owner=wire.Owner(pid, start))
            with fixture.loop_turn():
                grant = only_admitted(fixture.server.handle_request(outer))
            marker = f"{grant.reservation}\t{pid}\t{fixture.state_dir}\t{fixture.socket_path}"
            nested = fixture.request()
            with (mock.patch.dict(os.environ, {"BUILDLOG_RESERVED_STEP": marker}),
                  mock.patch.object(client, "_acquire", return_value=wire.Refused("fixture admission")) as acquire):
                self.assertIsInstance(client.outer_reservation(match_cpu_pool=True), client.OuterReservation)
                fixture.fault.stage = "after_state_write"
                with self.assertRaisesRegex(InjectedCrash, "after_state_write"):
                    fixture.release(grant)
                self.assertIsInstance(fixture.server.reservations[outer.episode].ending, service.ReleaseStatus)
                self.assertIsInstance(client.outer_reservation(match_cpu_pool=True), client.NoOuterReservation)
                self.assertEqual(client.admit(nested, lambda _wait: None, lambda: nested), wire.Refused("fixture admission"))
            acquire.assert_called_once()

    def test_placement_status_cli_reads_current_closed_and_unknown_over_the_socket(self) -> None:
        from scripts.build_queue import admission_wire as status_wire, placement
        from scripts.build_queue.test_placement import placement_request

        with AdmissionFixture() as fixture:
            request = placement_request(fixture)
            destination = request.constraints.candidates[0].destination
            assignment = placement.PlacementAssignment(request.episode, "attempt-a", destination,
                request.constraints.source_key, request.owner, request.constraints.session, request.since)
            claim = placement.ProviderClaimReference(destination.provider, destination.target, destination.scope,
                "claim-a", assignment.episode, assignment.attempt)
            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            cases = ((placement.CurrentAssignment(), claim, "current", 0, "claim-a", "-"),
                (placement.CurrentAssignment(), placement.NoProviderClaim(), "current", 0, "-", "-"),
                (placement.ClosedAssignment("source changed"), claim, "closed", 1, "claim-a", "source changed"))
            for state, provider, word, code, claim_id, reason in cases:
                with self.subTest(state=state), fixture.loop_turn():
                    fixture.server.assignments[key] = records.SavedPlacementAssignment(assignment, state, provider=provider)
                with fixture.serving(), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(client.main(["placement-status", "--episode", assignment.episode,
                        "--attempt", assignment.attempt]), code)
                owner = assignment.owner
                self.assertEqual(output.getvalue(), f"{word}\tmac\t{owner.host}:{owner.pid}:{owner.start_time}"
                    + f"\tsubmitting-session\t{claim_id}\t{reason}\n")
            with fixture.serving(), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(client.main(["placement-status", "--episode", assignment.episode,
                    "--attempt", "unseen"]), 1)
                self.assertEqual(client.placement_status("unseen", "unseen"),
                    status_wire.PlacementStatusReply(status_wire.UnknownPlacementStatus("unseen", "unseen")))
            self.assertEqual(output.getvalue(), "unknown\t-\t-\t-\t-\t-\n")

    def test_placement_status_cli_returns_two_when_the_service_is_unreachable(self) -> None:
        with AdmissionFixture() as fixture, mock.patch.dict(os.environ,
                {"ADMISSION_SOCKET": str(fixture.socket_dir / "missing.sock")}), \
                redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(client.main(["placement-status", "--episode", "episode-a", "--attempt", "attempt-a"]), 2)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("admission service unavailable: no socket", errors.getvalue())

    def test_cancel_placement_cli_removes_the_queued_episode(self) -> None:
        from scripts.build_hold.owning_session import NoOwningSession
        from scripts.build_queue import placement
        from scripts.mac_test.claim_state import WHOLE_MAC

        with AdmissionFixture() as fixture:
            occupied = fixture.occupy_host()
            fixture.free(0)
            local = fixture.request()
            owner = placement.SubmittingOwner(fixture.server.host, local.owner.pid, local.owner.start_time)
            destination = placement.PlacementDestination("local", "natedev", fixture.server.host, WHOLE_MAC)
            constraints = placement.PlacementConstraints(execution_identity.SourceKeyKnown("private-source"),
                ("cargo", "check"), str(fixture.root), "sample", NoOwningSession(),
                (placement.PlacementCandidate(destination),))
            request = wire.PlacementRequest(local.episode, owner, local.since, local.profile, constraints,
                local=wire.LocalAdmissionContext(local.argv, local.cwd, local.sidecar, local.call,
                    local.test_width, local.fresh_test))
            with fixture.serving(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertIsInstance(client.request_placement(request), wire.PlacementWait)
                self.assertIn(request.episode, fixture.server.waiters)
                self.assertEqual(client.main(["cancel-placement", "--episode", request.episode,
                    "--pid", str(owner.pid), "--reason", "the source changed"]), 0)
                self.assertNotIn(request.episode, fixture.server.waiters)
                self.assertNotIn(request.episode, fixture.server.placement_requests)
                fixture.release(occupied)

    def test_wait_subject_uses_the_usable_repository_and_step_fields(self) -> None:
        for repository, step, subject in (
            ("—", "nextest", "nextest"),
            ("$REPO", "nextest", "nextest"),
            ("repository", "unregistered session", "repository"),
            ("repository", "—", "repository"),
            ("unregistered session", "$STEP", "build"),
            ("repository", "step", "repository step"),
        ):
            with self.subTest(repository=repository, step=step):
                snapshot = replace(WAIT.snapshot, repo=repository, step=step)
                with redirect_stderr(io.StringIO()) as output:
                    client.WaitOutput()(replace(WAIT, snapshot=snapshot))
                self.assertIn(f"{subject} needs 8.0 GiB", output.getvalue().split("; "))

    def test_older_wait_lines_normalize_the_head_name_and_label(self) -> None:
        blocker: records.Waiter = {"pid": 7, "start_time": "7", "need": 2 * GIB, "since": 1,
                                  "session_name": "—", "step": "  ", "unit": "—"}
        for label in ("", " \t", "—", "$CALLER", "  other   caller  "):
            name = "other caller" if label == "  other   caller  " else "unregistered session"
            for order in (records.NoOrderHold(), records.HeldBehindHead(blocker)):
                with self.subTest(label=label, order=order):
                    decision = replace(WAIT.snapshot.decision, order_hold=order)
                    snapshot = replace(WAIT.snapshot, decision=decision,
                                       head_reason=records.BehindHead(label, 1, ""))
                    waiting = replace(WAIT, position=2, text="behind #1", snapshot=snapshot)
                    with redirect_stderr(io.StringIO()) as output:
                        client.WaitOutput()(waiting)
                    expected = (f"reason: behind #1 · head {name} needs 2.0 GiB"
                                if isinstance(order, records.HeldBehindHead) else f"reason: behind {name}")
                    self.assertIn(expected, output.getvalue().split("; "))
            with self.subTest(head_label=label):
                snapshot = replace(WAIT.snapshot, head_need=2 * GIB, head_label=label)
                with redirect_stderr(io.StringIO()) as output:
                    client.WaitOutput()(replace(WAIT, position=2, snapshot=snapshot))
                self.assertIn(f"head {name} needs 2.0 GiB", output.getvalue().split("; "))

    def test_wait_output_matches_the_service_name_after_reply_decoding(self) -> None:
        with AdmissionFixture() as fixture:
            head = fixture.request(need=2 * GIB)
            waiter: records.Waiter = {"pid": head.owner.pid, "start_time": head.owner.start_time,
                "since": head.since, "need": 2 * GIB, "session_name": "builder", "unit": "panes",
                "folder": "/private/widget", "step": "nextest"}
            service_name = service.wait_line_name(service.WaitingAdmission(head, waiter, service.NO_CONNECTION))
            self.assertEqual(service_name, "panes · nextest")

            older: records.Waiter = {"pid": head.owner.pid, "start_time": head.owner.start_time,
                                     "since": head.since, "need": 2 * GIB}
            for row, label in ((waiter, service_name), (older, "panes")):
                with self.subTest(row=row):
                    snapshot = replace(WAIT.snapshot,
                        decision=replace(WAIT.snapshot.decision, order_hold=records.HeldBehindHead(row)),
                        head_label=service_name, head_reason=records.BehindHead("panes", head.since, ""))
                    waiting = replace(WAIT, position=2, text="behind #1", snapshot=snapshot)
                    decoded = wire.parse_reply(wire.encode_reply(waiting))
                    assert isinstance(decoded, wire.Wait)
                    with redirect_stderr(io.StringIO()) as output:
                        client.WaitOutput()(decoded)
                    reason, = [part for part in output.getvalue().split("; ") if part.startswith("reason: ")]
                    self.assertEqual(reason, f"reason: behind #1 · head {label} needs 2.0 GiB")

    def test_wait_lines_name_the_head_and_identify_the_current_build(self) -> None:
        for position, label, expected, reason in (
            (2, "hana CI · hana-linux-2 · test", "head hana CI · hana-linux-2 · test needs 12.3 GiB", ""),
            (2, "compiler · check", "head compiler · check needs 12.3 GiB", ""),
            (2, "hana · test", "head hana · test needs 12.3 GiB", ""),
            (2, "42 · test", "head 42 · test needs 12.3 GiB", ""),
            (1, "hana CI · hana-linux-2 · test", "this build is the queue head", ""),
            (1, "hana · test", "this build is the queue head", "it edits the merged checkout"),
            (3, "first · nextest", "behind #2 · compiler · nextest", ""),
        ):
            with self.subTest(position=position, label=label), AdmissionFixture() as fixture:
                waiting = replace(WAIT, position=position, head=label,
                    snapshot=replace(WAIT.snapshot, head_need=int(12.3 * GIB), head_label=label))
                if position == 3:
                    fixture.hold_starts()
                    first = fixture.request(need=GIB, since=fixture.clock.now() - 2)
                    _ = fixture.server.handle_request(replace(first, identity=replace(first.identity, name="first")))
                    nextest = fixture.request(need=18 * GIB, since=fixture.clock.now() - 1)
                    _ = fixture.server.handle_request(replace(nextest,
                        identity=replace(nextest.identity, name="second", unit="compiler")))
                    follower = fixture.request(need=GIB)
                    _ = fixture.server.handle_request(follower)
                    fixture.free(15 * GIB)
                    _ = fixture.run_pass()
                    self.assertIn(first.episode, fixture.server.reservations)
                    reply = fixture.server.waiters[follower.episode].last_reply
                    assert isinstance(reply, wire.Wait)
                    received = wire.parse_reply(wire.encode_reply(reply))
                    assert isinstance(received, wire.Wait)
                    waiting = received
                    self.assertEqual(waiting.position, position)
                with mock.patch.object(client, "CLOCK", fixture.clock), redirect_stderr(io.StringIO()) as output:
                    client.WaitOutput(local_reason=reason)(waiting)
                self.assertIn(expected, output.getvalue())
                if position == 3:
                    self.assertNotIn("head first", output.getvalue())
                    self.assertNotIn("queue head", output.getvalue())
                if reason:
                    self.assertIn(f"runs on natedev: {reason}", output.getvalue())
                else:
                    self.assertNotIn("runs on natedev:", output.getvalue())
                self.assertNotIn("head  needs", output.getvalue())
                if position == 1:
                    self.assertNotIn(f"head {label} needs", output.getvalue())

    def service_wait_record(self) -> tuple[dict[str, object], dict[str, object]]:
        raw = cast(dict[str, object], json.loads(wire.encode_reply(replace(WAIT, snapshot=service_snapshot()))))
        snapshot = cast(dict[str, object], cast(dict[str, object], raw["data"])["snapshot"])
        return raw, snapshot

    def test_unknown_unused_service_variants_keep_the_wait_and_allow_admission(self) -> None:
        for field in ("job_state", "service_job_state"):
            with self.subTest(field=field), AdmissionFixture() as fixture:
                request = fixture.request()
                raw, snapshot = self.service_wait_record()
                memory = cast(dict[str, object], cast(list[dict[str, object]], snapshot["running"])[0]["measured"])
                memory[field] = {"kind": "FutureServiceJob"}
                transport = self.use_transport(fixture, Exchange((json.dumps(raw) + "\n").encode(),
                                                                 TimedChunk(0.01, reply_bytes(GRANTED))))
                waits: list[wire.Wait] = []
                with redirect_stderr(io.StringIO()) as output:
                    result = client.admit(request, waits.append, lambda: request)
                self.assertEqual(result, GRANTED)
                self.assertEqual(len(waits), 1)
                measured = waits[0].snapshot.running[0].measured
                assert isinstance(measured, records.UnownedSliceMemory)
                self.assertEqual(measured.bytes, 123)
                self.assertEqual(transport.requests, [request])
                self.assertNotIn("memory admission check failed", output.getvalue())
                self.doCleanups()

    def test_unknown_used_wait_variant_keeps_the_named_checker_error(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            raw, snapshot = self.service_wait_record()
            snapshot["head_reason"] = {"kind": "FutureAdmissionReason"}
            transport = self.use_transport(fixture, Exchange((json.dumps(raw) + "\n").encode()))
            with redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, wire.Refused("invalid snapshot variant"))
            self.assertIn("memory admission check failed (invalid snapshot variant); step refused", output.getvalue())
            self.assertEqual(transport.requests, [request])

    def test_unused_service_variants_do_not_extend_the_silent_queue_deadline(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.service_request()
            raw, snapshot = self.service_wait_record()
            memory = cast(dict[str, object], cast(list[dict[str, object]], snapshot["running"])[0]["measured"])
            memory["service_job_state"] = {"kind": "FutureServiceJob"}
            transport = self.use_transport(fixture, Exchange((json.dumps(raw) + "\n").encode(), TimeoutError()))
            waits: list[wire.Wait] = []
            with redirect_stderr(io.StringIO()) as output:
                result = client.service_admit(request, waits.append, wire.AcquisitionTiming(10, 10, 5))
            self.assertEqual(result, wire.Refused(
                "build queue unreachable for 5 s; step stopped; cargo token given back"))
            self.assertEqual(fixture.clock.monotonic(), 5)
            self.assertEqual(len(waits), 1)
            self.assertEqual(transport.requests, [request])
            self.assertNotIn("memory admission check failed", output.getvalue())

    def test_history_generation_reads_current_configuration_at_use(self) -> None:
        with AdmissionFixture() as fixture:
            transport = self.use_transport(fixture, Exchange(reply_bytes(wire.BuildHistoryReply(7))))
            with mock.patch.object(records, "read_memory_config", wraps=records.read_memory_config) as read:
                generation = client.build_history_generation(client.READ_CONFIGURATION_AT_USE)
            self.assertEqual(generation, records.BuildHistoryGeneration(7))
            self.assertEqual(len(transport.requests), 1)
            self.assertIsInstance(transport.requests[0], wire.BuildHistoryRequest)
            read.assert_called_once()

    def test_history_generation_uses_supplied_configuration_without_another_read(self) -> None:
        with AdmissionFixture() as fixture:
            configuration = records.read_memory_config()
            transport = self.use_transport(fixture, Exchange(reply_bytes(wire.BuildHistoryReply(8))))
            with mock.patch.object(records, "read_memory_config", side_effect=AssertionError("supplied configuration was not used")):
                generation = client.build_history_generation(configuration)
            self.assertEqual(generation, records.BuildHistoryGeneration(8))
            self.assertEqual(len(transport.requests), 1)
            self.assertIsInstance(transport.requests[0], wire.BuildHistoryRequest)

    def test_history_generation_refuses_an_unusable_current_configuration(self) -> None:
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", silence_seconds="invalid")
            with redirect_stderr(io.StringIO()), self.assertRaises(client.CheckerFailure) as error:
                _ = client.build_history_generation(client.READ_CONFIGURATION_AT_USE)
            self.assertIn("silence_seconds", str(error.exception))

    def test_cloud_admit_uses_the_private_service_and_releases_its_slot(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            request = replace(fixture.request(step="gate"), place=wire.CloudPlace("aws"))
            with fixture.serving():
                result = client.admit(request, lambda _wait: None, lambda: request)
                assert isinstance(result, wire.Admitted)
                self.assertEqual(result.need, 0)
                self.assertEqual(client.release(result.reservation, 0, result.cleanup_timing), wire.Ok())
            self.assertTrue(fixture.callers_released())

    def test_cloud_place_survives_client_reconnect_and_estimate_refresh(self) -> None:
        with AdmissionFixture() as fixture:
            request = replace(fixture.request(step="gate"), place=wire.CloudPlace("aws"))
            transport = self.use_transport(fixture,
                Exchange(reply_bytes(EXPIRED), TimedChunk(0.1, b"")),
                Exchange(reply_bytes(GRANTED)))
            refreshed = replace(request, place=wire.LocalPlace())
            with redirect_stderr(io.StringIO()):
                result = client.admit(request, lambda _wait: None, lambda: refreshed)
            self.assertEqual(result, GRANTED)
            self.assertGreaterEqual(len(transport.requests), 2)
            for sent in transport.requests:
                assert isinstance(sent, wire.AdmitRequest)
                self.assertEqual(sent.place, wire.CloudPlace("aws"))

    def test_import_does_not_load_memory_admission(self) -> None:
        result = subprocess.run([sys.executable, "-c",
            "import sys\nfrom scripts.build_queue import admission_client\nprint('\\n'.join(sys.modules))"],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, check=True)
        self.assertNotIn("scripts.lint.memory_admit", result.stdout.splitlines())

    def test_outage_message_reaches_builds_without_herdr_on_path(self) -> None:
        with short_socket_directory() as directory:
            root = Path(directory)
            binaries = root / "bin"
            binaries.mkdir()
            for name in ("ps", "ssh"):
                executable = binaries / name
                _ = executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            home = root / "home"
            daemon_home = home / ".codex"
            notes = root / "notes"
            write_codex_account(daemon_home, notes, "codex1")
            address = daemon_home / "app-server-control/control.sock"
            address.parent.mkdir()
            sessions = root / "sessions"
            sessions.mkdir()
            identity = fixture_runtime_id("outage-builds")
            environment = {"HOME": str(home), "PATH": str(binaries), "CLAUDE_PYTHON": sys.executable,
                "AGENT_NOTES_DIR": str(notes), "ROSTER_CODEX_SOCKET": str(address),
                "NOTIFIER_SESSIONS_DIR": str(sessions), "NOTIFIER_STATE_DIR": str(root / "notifier"),
                "ROSTER_RUN_ROOT": str(root / "runs"), "HERDR_PROC": str(root / "proc"),
                "DESIGN_ROLES_STATE": str(root / "roles"), "XDG_STATE_HOME": str(root / "state"),
                **config_environment(root / "config")}
            direct_config = root / "config/direct.conf"
            _ = direct_config.write_text(direct_config.read_text())
            received: list[dict[str, object]] = []

            def receive(connection: socket.socket) -> None:
                initialize = receive_frame(connection)
                self.assertEqual(initialize["method"], "initialize")
                reply(connection, initialize, {})
                self.assertEqual(receive_frame(connection)["method"], "initialized")
                while True:
                    try:
                        request = receive_frame(connection)
                    except EOFError:
                        return
                    if request["method"] == "thread/loaded/list":
                        reply(connection, request, {"data": [identity]})
                    elif request["method"] == "thread/read":
                        reply(connection, request, {"thread": {"id": identity, "name": "builds",
                            "cwd": str(root), "status": {"type": "idle"}}})
                    elif request["method"] == "thread/queue/add":
                        received.append(cast(dict[str, object], request["params"]))
                        reply(connection, request, {"queuedSubmission": {"id": "outage"}})
                    else:
                        self.fail(f"unexpected daemon method: {request['method']}")

            with mock.patch.dict(os.environ, environment, clear=True), \
                 isolated_staff_file(root / "staff.json", environment):
                document = root / "production.md"
                _ = document.write_text("# Builds fixture\n")
                runner = fixture_member("showrunner", name="builds", runtime_id=identity,
                    doc=staff.LiveDoc(roster.remote.local_machine(), document),
                    backend=staff.CodexBackend(daemon_home))
                parent = fixture_member("unit", name="pane-parent", parent=runner.id,
                    worktree=staff.PresentWorktree(roster.remote.local_machine(), root))
                _ = fixture_worker("reader", parent.id)
                command = session_send_argv(client.BUILDS_SESSION, sender="build-admission",
                    lint_settings=conf.read("lint"))
                child_environment = dict(os.environ)
                _ = child_environment.pop("TEST_SENDER_GUARD", None)
                _ = child_environment.pop("HERDR_BIN", None)
                child_environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "lib/python_startup")
                message = "The build queue service has not answered. Builds wait."
                with FakeDaemon(address, receive, repeat=True):
                    result = subprocess.run(command, input=message, text=True, capture_output=True,
                        env=child_environment, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            outcome = send_result.send_outcome(result.returncode, result.stdout)
            self.assertIsInstance(outcome, send_result.ConfirmedSendOutcome)
            assert isinstance(outcome, send_result.ConfirmedSendOutcome)
            self.assertEqual(outcome.header.outcome, "sent")
            delivery, = received
            self.assertEqual(delivery["threadId"], identity)
            payload, = cast(list[dict[str, str]], delivery["input"])
            self.assertEqual(payload.keys(), {"type", "text"})
            self.assertEqual(payload["type"], "text")
            prefix = message + "\nSender native id: unavailable\nSent: "
            self.assertRegex(payload["text"], r"\A" + re.escape(prefix) + r"\d{2}:\d{2} [^\n]+\Z")

    def test_runner_width_change_uses_its_account_and_changes_its_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            uid = os.getuid() + 10000
            owner = fixture.account_owner(uid)
            request = replace(fixture.request(owner=owner), episode=f"uid:{uid}:width-owner")
            with fixture.peer_account(uid):
                grant = only_admitted(fixture.server.handle_request(request))
                with fixture.serving(), mock.patch.object(client, "os", wraps=os) as runner_os, \
                     mock.patch.object(runner_os, "getuid", return_value=uid):
                    self.assertEqual(client.width(grant.reservation, 3), wire.Ok())
                self.assertEqual(fixture.server.reservations[request.episode].test_width, records.RequestedTestWidth(3))
            fixture.release(grant)


    def test_runner_closed_connections_reach_the_acquisition_deadline_without_home_reads(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.service_request()
            transport = self.use_transport(fixture,
                *(Exchange(TimedChunk(0.125, b"")) for _ in range(8)))
            with mock.patch.object(records, "read_memory_config", side_effect=PermissionError("runner home")), \
                 mock.patch.object(step_estimator, "step_estimate", side_effect=PermissionError("runner history")), \
                 redirect_stderr(io.StringIO()) as output:
                result = client.service_admit(request, lambda _wait: None,
                    wire.AcquisitionTiming(1, 0.1, 0.3))
            self.assertIsInstance(result, wire.Refused)
            self.assertIn("build queue unreachable for 0.3 s", output.getvalue())
            self.assertGreaterEqual(fixture.clock.monotonic(), 0.3)
            self.assertGreater(len(transport.requests), 1)
            self.assertEqual(transport.requests, [request] * len(transport.requests))

    def test_a_ci_step_waits_through_a_natedev_hold_and_starts_after_the_release(self) -> None:
        # CI run 43737288332 refused four steps 60 s into the fps hold on 2026-11-15.
        held = wait_reply(records.NatedevHeld("fps", "Phase 3 timing"), 3, "",
                          "natedev is held by fps for Phase 3 timing; waiting for release")
        for reason, granted in ((held.reason, True), (records.ReadingUnavailable("natedev hold state is unreadable"), False)):
            with self.subTest(reason=type(reason).__name__), AdmissionFixture() as fixture:
                request = fixture.service_request()
                reply = replace(held, reason=reason)
                exchange = Exchange(reply_bytes(reply), *(TimedChunk(5, reply_bytes(reply)) for _ in range(24)))
                transport = self.use_transport(fixture, exchange)
                waits: list[wire.Wait] = []

                def wait_for_release(wait: wire.Wait) -> None:
                    waits.append(wait)
                    if len(waits) == 25:
                        exchange.events.append(reply_bytes(GRANTED))

                with redirect_stderr(io.StringIO()) as output:
                    result = client.service_admit(request, wait_for_release, wire.AcquisitionTiming(10, 10, 5))
                if granted:
                    self.assertEqual(result, GRANTED)
                    self.assertEqual(fixture.clock.monotonic(), 120)
                    self.assertEqual(len(waits), 25)
                    self.assertEqual(transport.requests, [request])
                    self.assertNotIn("memory admission check failed", output.getvalue())
                else:
                    self.assertEqual(result, wire.Refused("natedev hold state is unreadable"))
                    self.assertIn("natedev hold state is unreadable); step refused", output.getvalue())
                self.doCleanups()

    def test_runner_wait_recovers_and_cleanup_uses_its_own_timing_without_home_reads(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.service_request()
            granted = replace(GRANTED, cleanup_timing=wire.CleanupTiming(7, 2))
            transport = self.use_transport(fixture,
                Exchange(TimedChunk(0.1, b"")),
                Exchange(TimedChunk(0.1, reply_bytes(WAIT)), TimedChunk(0.2, b"")),
                Exchange(reply_bytes(granted)), Exchange(reply_bytes(wire.Ok())))
            waits: list[wire.Wait] = []
            with mock.patch.object(records, "read_memory_config", side_effect=PermissionError("runner home")), \
                 mock.patch.object(step_estimator, "step_estimate", side_effect=PermissionError("runner history")), \
                 redirect_stderr(io.StringIO()) as output:
                result = client.service_admit(request, waits.append, wire.AcquisitionTiming(1, 0.1, 0.3))
                self.assertEqual(result, granted)
                self.assertEqual(client.release(granted.reservation, 137, granted.cleanup_timing), wire.Ok())
            self.assertEqual(waits, [WAIT])
            self.assertEqual(transport.requests[:3], [request] * 3)
            release = transport.requests[-1]
            self.assertIsInstance(release, wire.ReleaseRequest)
            if isinstance(release, wire.ReleaseRequest):
                self.assertEqual((release.reservation, release.status), (granted.reservation, 137))
            self.assertEqual(transport.timeouts[-1], 7)
            self.assertNotIn("step stopped", output.getvalue())

    def test_wait_events_name_each_holder_change_without_repeating_an_unchanged_holder(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            holders: tuple[records.WaitHolder, ...] = (
                records.SessionHolder("first-id", "first-name"),
                records.SessionHolder("first-id", "first-name"),
                records.SessionHolder("second-id", "second-name"),
                records.UnitHolder("compiler"), records.NoHolder())
            waits = [replace(WAIT, snapshot=replace(WAIT.snapshot, holder=holder)) for holder in holders]
            interval = records.read_memory_config(report=False).wait_reply_seconds
            events = [TimedChunk(interval + 1, reply_bytes(waits[1])),
                      *(reply_bytes(wait) for wait in waits[2:]), TimedChunk(0.01, reply_bytes(GRANTED))]
            transport = self.use_transport(fixture, Exchange(reply_bytes(waits[0]), *events))
            with mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                    request.profile, step_estimator.ReusableEstimate())), \
                 redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()) as output:
                status = shell_place.main(["admit", "--pid", str(request.owner.pid), "--wait-events", "--", "cargo", "check"])
            self.assertEqual(status, 0)
            lines = output.getvalue().splitlines()
            self.assertEqual((lines[0], lines[-1]), ("first", "done"))
            self.assertTrue(all(line.startswith("holder ") for line in lines[1:-1]))
            self.assertEqual([json.loads(line.removeprefix("holder ")) for line in lines[1:-1]], [
                {"kind": "session", "session_id": "first-id", "session_name": "first-name"},
                {"kind": "session", "session_id": "second-id", "session_name": "second-name"},
                {"kind": "unit", "unit": "compiler"}, {"kind": "none"}])
            self.assertEqual(len(transport.requests), 1)

    def test_a_wait_reply_without_a_holder_keeps_the_wait_and_reports_no_holder(self) -> None:
        real_socket = socket.socket
        with AdmissionFixture() as fixture:
            request = fixture.request()
            record = cast(dict[str, object], json.loads(wire.encode_reply(WAIT)))
            data_record = cast(dict[str, object], record["data"])
            snapshot = cast(dict[str, object], data_record["snapshot"])
            del snapshot["holder"]
            data = (json.dumps(record) + "\n").encode()
            transport = self.use_transport(fixture, Exchange(data, TimedChunk(0.01, reply_bytes(GRANTED))))
            with mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                    request.profile, step_estimator.ReusableEstimate())), \
                 redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()) as output:
                status = shell_place.main(["admit", "--pid", str(request.owner.pid), "--wait-events", "--", "cargo", "check"])
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue().splitlines(), ["first", 'holder {"kind":"none"}', "done"])
            self.assertEqual(len(transport.requests), 1)

        from scripts.build_queue import placement

        hold_seconds = 30
        for execution_target in ("local", "mac", "mac-handoff", "mac-retry-notice", "mac-truncated-assignment", "mac-kept-claim"):
            with mock.patch("scripts.build_queue.admission_client.socket.socket", real_socket), \
                 AdmissionFixture() as fixture, mock.patch.object(client, "CLOCK", fixture.clock):
                if execution_target == "mac-kept-claim":
                    fixture.configure(remote_start_hold_seconds=hold_seconds)
                if execution_target != "local":
                    _ = subprocess.run(["git", "init", "-q", str(fixture.root)], capture_output=True, check=True)
                    _ = (fixture.root / "Cargo.toml").write_text('[package]\nname="sample"\nversion="0.1.0"\n')
                    _ = write_config_fixture(fixture.root / "mac_test.conf",
                        f"offload=on\nlinux_only.{fixture.root.name}=\n", template="mac_test")
                    _ = (fixture.root / "build_targets.conf").write_text(
                        "targets=natedev,mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
                        + "kind.natedev=local\nplatform.natedev=linux\n")
                occupied = fixture.occupy_host()
                fixture.free(0)
                request = fixture.request()
                finish_probe = threading.Event()
                local_evidence_ready = threading.Event()

                def provider(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                    if key.destination.provider == "local":
                        if not local_evidence_ready.is_set():
                            return placement.HostEvidenceUnavailable(key, fixture.clock.now(), "the local evidence is not ready")
                        return placement.FreshHostEvidence(key, fixture.clock.now(), "", placement.AllowedHost())
                    if not finish_probe.wait(10):
                        raise AssertionError("the test did not release its private evidence reader")
                    return placement.FreshHostEvidence(key, fixture.clock.now(), "", placement.AllowedHost())

                fixture.server.host_probe = provider
                assignment_file = fixture.root / "assignment.json"
                arguments = ["place-step", "--once", "--wait-events", *(["--local-only"] if execution_target == "local" else []),
                    "--pid", str(request.owner.pid), "--episode", request.episode,
                    "--since", str(request.since), "--repo-root", str(fixture.root),
                    "--assignment-file", str(assignment_file), "--", "cargo", "check"]
                with fixture.serving(), \
                     mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                         request.profile, step_estimator.ReusableEstimate())), \
                     redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()) as output:
                    try:
                        self.assertEqual(client.main(arguments), 78, errors.getvalue())
                        self.assertEqual((output.getvalue(), errors.getvalue()), ("", ""))
                        kept_paths = mac_test.StatePaths(fixture.root / "private-mac")
                        kept_claim = kept_paths.claims / (fixture.root.name + ".json")
                        kept_bytes = b""
                        if execution_target == "mac-kept-claim":
                            kept_paths.claims.mkdir(parents=True)
                            started = process_identity.process_start(os.getpid())
                            assert isinstance(started, process_identity.Started)
                            kept_record: claim_state.RemoteJobClaim = {
                                "claim_id": "kept-claim", "episode": "old-episode", "attempt": "old-attempt",
                                "submitting_session": "old-session", "pid": os.getpid(), "proc_start": started.start,
                                "what": "check older step", "worktree": str(fixture.root),
                                "since": "2026-11-17T15:42:00+00:00", "remote_host": "fixture-mac"}
                            mac_test.write_json(kept_claim, kept_record)
                            with mock.patch.object(mac_test, "target_claim_scope",
                                    return_value=claim_state.RepositoryScope(fixture.root.name)):
                                self.assertEqual(mac_test.free_now(kept_paths, fixture.root / "mac_test.conf",
                                    fixture.root, session=NoOwningSession()), "busy")
                            kept_record["proc_start"] = "ended-owner"
                            mac_test.write_json(kept_claim, kept_record)
                            kept_bytes = kept_claim.read_bytes()
                        with fixture.loop_turn():
                            submitted = fixture.server.placement_requests[request.episode]
                            local_evidence_ready.set()
                            if execution_target != "local":
                                remote = next(candidate.destination for candidate in submitted.constraints.candidates
                                              if candidate.destination.provider == "mac")
                                fixture.server.publish_host_evidence(placement.FreshHostEvidence(
                                    fixture.server.host_evidence_key(submitted, remote), fixture.clock.now(), "",
                                    placement.TemporaryExclusion("busy")))
                        _ = fixture.run_pass()
                        self.assertEqual(client.main(arguments), 78)
                        lines = output.getvalue().splitlines()
                        self.assertEqual(lines[0], "first")
                        self.assertEqual(len(lines), 2)
                        self.assertTrue(lines[1].startswith("holder "), lines)
                        self.assertIn("waiting for memory", errors.getvalue())
                        self.assertFalse(fixture.caller_reservations())
                        _ = output.seek(0)
                        _ = output.truncate()
                        _ = errors.seek(0)
                        _ = errors.truncate()
                        self.assertEqual(client.main(arguments), 78)
                        self.assertEqual((output.getvalue(), errors.getvalue()), ("", ""))
                        wait_state = assignment_file.with_name(assignment_file.name + ".wait")
                        _ = wait_state.write_text('{"kind":')
                        self.assertEqual(client.main(arguments), 78, errors.getvalue())
                        _ = output.seek(0)
                        _ = output.truncate()
                        _ = errors.seek(0)
                        _ = errors.truncate()
                        if execution_target == "local":
                            fixture.free(60 * GIB)
                        else:
                            allowed: placement.HostEligibility = placement.AllowedHost()
                            if execution_target == "mac-kept-claim":
                                with mock.patch.object(mac_test, "target_claim_scope",
                                        return_value=claim_state.RepositoryScope(fixture.root.name)):
                                    free = mac_test.free_now(kept_paths, fixture.root / "mac_test.conf",
                                        fixture.root, session=NoOwningSession())
                                self.assertEqual(free, "free")
                                allowed = placement.AllowedHost() if free == "free" else placement.TemporaryExclusion(free)
                                self.assertEqual(submitted.route, target_config.ANY_ELIGIBLE_TARGET)
                                self.assertTrue(all(isinstance(candidate.eligibility, placement.AllowedHost)
                                    for candidate in submitted.constraints.candidates))
                            with fixture.loop_turn():
                                remote = next(candidate.destination for candidate in submitted.constraints.candidates
                                              if candidate.destination.provider == "mac")
                                fixture.server.publish_host_evidence(placement.FreshHostEvidence(
                                    fixture.server.host_evidence_key(submitted, remote), fixture.clock.now(), "", allowed))
                        _ = fixture.run_pass()
                        self.assertEqual(client.main(arguments), 0)
                        lines = output.getvalue().splitlines()
                        if execution_target == "local":
                            self.assertEqual(lines[0], "done")
                            self.assertTrue(lines[1].startswith("local\tnatedev\t"), lines)
                            self.assertEqual(client.read_assignment(assignment_file).assignment.destination.provider, "local")
                            grant = fixture.server.ledger[request.episode].reply
                            assert isinstance(grant, wire.Admitted)
                            fixture.release(grant)
                        else:
                            self.assertTrue(lines[0].startswith("left another machine is free after "), lines)
                            self.assertTrue(lines[-1].startswith("mac\tmac\t"), lines)
                            self.assertNotIn("another machine is free", errors.getvalue())
                            self.assertFalse(fixture.caller_reservations())
                            saved_remote = client.read_assignment(assignment_file).assignment
                            if execution_target == "mac-kept-claim":
                                self.assertEqual(kept_claim.read_bytes(), kept_bytes)
                                with fixture.loop_turn():
                                    self.assertIn(submitted.episode, fixture.server.waiters)
                                    fixture.clock.advance(hold_seconds)
                                    _ = fixture.run_pass()
                                    self.assertNotIn(submitted.episode, fixture.server.waiters)
                            if execution_target == "mac-truncated-assignment":
                                _ = assignment_file.write_text('{"v":3,"type":')
                                self.assertEqual(client.main(arguments), 0, errors.getvalue())
                                recovered = client.read_assignment(assignment_file).assignment
                                self.assertEqual(recovered, saved_remote)
                                self.assertEqual((recovered.episode, recovered.owner),
                                                 (submitted.episode, submitted.owner))
                            with fixture.loop_turn():
                                fixture.server.publish_host_evidence(placement.FreshHostEvidence(
                                    fixture.server.host_evidence_key(submitted, saved_remote.destination), fixture.clock.now(), "",
                                    placement.TemporaryExclusion("busy")))
                            if execution_target == "mac-handoff":
                                self.assertEqual(client.main(["report-prerequisite", "--assignment-file",
                                    str(assignment_file), "--token", "after-grant"]), 0)
                                fixture.free(60 * GIB)
                            self.assertEqual(client.main(["claim-failed", "--assignment-file", str(assignment_file),
                                                          "--reason", "lost" if execution_target == "mac-retry-notice" else "busy"]), 0)
                            if execution_target == "mac-handoff":
                                # The replacement needs no token check, so the next pass grants natedev.
                                self.assertTrue(fixture.caller_reservations())
                            else:
                                self.assertFalse(fixture.caller_reservations())

                            if execution_target == "mac-retry-notice":
                                assignment_file.unlink()
                                assignment_file.with_name(assignment_file.name + ".queued").unlink(missing_ok=True)
                                _ = output.seek(0)
                                _ = output.truncate()
                                previous = fixture.server.handle_request
                                observed: list[wire.Reply] = []

                                def free_after_first_reply(request: wire.Request,
                                        connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
                                    if isinstance(request, wire.PrerequisiteReport):
                                        fixture.free(60 * GIB)
                                    replies = previous(request, connection)
                                    if isinstance(request, wire.PlacementRequest):
                                        observed.extend(replies)
                                    return replies

                                with mock.patch.object(fixture.server, "handle_request", side_effect=free_after_first_reply):
                                    self.assertEqual(client.main(arguments), 0, errors.getvalue())
                                self.assertEqual(len(observed), 2, observed)
                                self.assertIsInstance(observed[0], wire.PlacementWait)
                                self.assertIsInstance(observed[1], wire.LocalPlacementGranted)
                                self.assertEqual(output.getvalue().count("retry verify.sh: Mac attempt ended (lost); "), 1)
                                self.assertIn("waiting 180 s before each retry", output.getvalue())
                            else:
                                def release_before_retry() -> None:
                                    fixture.free(60 * GIB)

                                token_arguments = [*arguments[:arguments.index("--")], "--token", "after-grant",
                                                   *arguments[arguments.index("--"):]]
                                # After its first pass, a caller without its token can get a local grant;
                                # it takes the token after the grant. The service marks every TokenRequired
                                # row token-waiting, as a service from before this client does.
                                with mock.patch.object(fixture.server, "before_admission", side_effect=release_before_retry):
                                    self.assertEqual(client.main(token_arguments), 0, errors.getvalue())
                            retry_grant = client.read_assignment(assignment_file)
                            self.assertIsInstance(retry_grant, wire.LocalPlacementGranted)
                            assert isinstance(retry_grant, wire.LocalPlacementGranted)
                            self.assertEqual(retry_grant.assignment.episode, submitted.episode)
                            self.assertNotEqual(retry_grant.assignment.attempt, saved_remote.attempt)
                            fixture.release(retry_grant.grant)
                        fixture.release(occupied)
                    finally:
                        finish_probe.set()

        cases = (
            (("--mac-only",), placement.TemporaryExclusion("busy"), 78),
            (("--mac-only",), placement.TemporaryExclusion("blocked"), 2),
            (("--mac-only",), placement.TemporaryExclusion("disk"), 2),
            (("--mac-only",), placement.TemporaryExclusion("unreachable"), 2),
            (("--mac-only",), placement.TemporaryExclusion("backoff"), 2),
            (("--mac-only",), placement.TemporaryExclusion("off"), 2),
            (("--mac-only",), placement.FixedExclusion("repo"), 2),
            (("--mac-only",), placement.FixedExclusion("linux_only"), 2),
            (("--target", "mac"), placement.TemporaryExclusion("busy"), 78),
            (("--target", "mac"), placement.TemporaryExclusion("blocked"), 78),
            (("--target", "mac"), placement.TemporaryExclusion("unreachable"), 78),
        )
        for selection, exclusion, expected in cases:
            with self.subTest(selection=selection, reason=exclusion.reason), \
                 mock.patch("scripts.build_queue.admission_client.socket.socket", real_socket), \
                 AdmissionFixture() as fixture, mock.patch.object(client, "CLOCK", fixture.clock):
                _ = subprocess.run(["git", "init", "-q", str(fixture.root)], capture_output=True, check=True)
                _ = (fixture.root / "Cargo.toml").write_text('[package]\nname="sample"\nversion="0.1.0"\n')
                _ = write_config_fixture(fixture.root / "mac_test.conf",
                    f"offload=on\nlinux_only.{fixture.root.name}=\n", template="mac_test")
                _ = (fixture.root / "build_targets.conf").write_text(
                    "targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
                    + "kind.natedev=local\nplatform.natedev=linux\n")
                request = fixture.request(step="check")
                finish_probe = threading.Event()
                _ = fixture.callback(finish_probe.set)

                def provider(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                    # Only the test supplies Mac evidence until the assertions finish.
                    if key.destination.target == "mac":
                        _ = finish_probe.wait()
                    return placement.HostEvidenceUnavailable(
                        key, fixture.clock.now(), "the test has not published provider evidence")

                fixture.server.host_probe = provider
                arguments = ["place-step", "--once", "--wait-events", *selection,
                    "--pid", str(request.owner.pid), "--episode", request.episode,
                    "--since", str(request.since), "--repo-root", str(fixture.root), "--package", "sample",
                    "--assignment-file", str(fixture.root / "assignment.json"), "--", "cargo", "check"]
                with fixture.serving(), \
                     mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                         request.profile, step_estimator.ReusableEstimate())), \
                     redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(client.main(arguments), 78, errors.getvalue())
                    with fixture.loop_turn():
                        submitted = fixture.server.placement_requests[request.episode]
                        destination = next(candidate.destination for candidate in submitted.constraints.candidates
                                           if candidate.destination.target == "mac")
                        fixture.server.publish_host_evidence(placement.FreshHostEvidence(
                            fixture.server.host_evidence_key(submitted, destination), fixture.clock.now(), "", exclusion))
                    _ = fixture.run_pass()
                    _ = output.seek(0)
                    _ = output.truncate()
                    self.assertEqual(client.main(arguments), expected, errors.getvalue())
                    if expected == 2:
                        self.assertEqual(output.getvalue(), f"refused\t{exclusion.reason}\n")
                    if selection == ("--target", "mac") and exclusion.reason in ("busy", "blocked"):
                        reply = client.request_placement(submitted)
                        self.assertIsInstance(reply, wire.PlacementWait)
                        assert isinstance(reply, wire.PlacementWait)
                        self.assertIsInstance(reply.wait, wire.PlacementResourceWait)
                        assert isinstance(reply.wait, wire.PlacementResourceWait)
                        self.assertEqual((reply.wait.resource, reply.wait.reason), ("mac", exclusion.reason))
                        self.assertFalse(fixture.server.current_assignments)
                    self.assertFalse(any(line.startswith("{") for line in output.getvalue().splitlines()))
                    self.assertFalse(fixture.server.reservations)

    def test_fixed_route_does_not_pin_a_step_with_an_eligible_remote(self) -> None:
        from scripts.build_queue import execution_identity, placement

        mac = placement.PlacementDestination("mac", "mac", "fixture-mac", claim_state.WHOLE_MAC)
        local = placement.PlacementDestination("local", "natedev", "fixture-local", claim_state.WHOLE_MAC)
        for eligibility in (placement.AllowedHost(), placement.FixedExclusion("full gate runs on natedev")):
            with self.subTest(eligibility=eligibility):
                constraints = placement.PlacementConstraints(execution_identity.SourceKeyKnown("source"),
                    ("cargo", "check"), "/fixture", "", NoOwningSession(),
                    (placement.PlacementCandidate(mac, eligibility), placement.PlacementCandidate(local)))
                expected = (target_config.LocalRequired(eligibility.reason)
                    if isinstance(eligibility, placement.FixedExclusion) else target_config.ANY_ELIGIBLE_TARGET)
                self.assertEqual(client.constrained_step_route(target_config.ANY_ELIGIBLE_TARGET, constraints), expected)
                for pinned in (target_config.MAC_ONLY, target_config.PinnedTarget("mac"), target_config.LOCAL_CALLER):
                    self.assertEqual(client.constrained_step_route(pinned, constraints), pinned)

    def test_final_waiter_records_its_fixed_local_route(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue import target_availability, memory_feed
        from datetime import UTC

        with AdmissionFixture() as fixture:
            _ = subprocess.run(["git", "init", "-q", str(fixture.root)], capture_output=True, check=True)
            _ = (fixture.root / "Cargo.toml").write_text('[package]\nname="sample"\nversion="0.1.0"\n')
            _ = write_config_fixture(fixture.root / "mac_test.conf",
                f"offload=on\nlinux_only.{fixture.root.name}=\n", template="mac_test")
            _ = (fixture.root / "build_targets.conf").write_text(
                "targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
                + "kind.natedev=local\nplatform.natedev=linux\n")
            request = fixture.request(step="check")
            fixture.server.host_probe = lambda key: placement.HostEvidenceUnavailable(
                key, fixture.clock.now(), "provider evidence is not ready")
            arguments = ["place-step", "--once", "--wait-events", "--kind", "final",
                "--pid", str(request.owner.pid), "--episode", request.episode, "--since", str(request.since),
                "--repo-root", str(fixture.root), "--assignment-file", str(fixture.root / "assignment.json"),
                "--", "cargo", "check", "--workspace", "--all-targets"]
            with fixture.serving(), mock.patch.object(step_estimator, "step_estimate",
                    return_value=step_estimator.StepEstimate(request.profile, step_estimator.ReusableEstimate())), \
                 redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
                self.assertEqual(client.main(arguments), 78, errors.getvalue())
                with fixture.loop_turn():
                    submitted = fixture.server.placement_requests[request.episode]
            self.assertEqual(submitted.route, target_config.LocalRequired("full gate runs on natedev"))
            mac = next(candidate for candidate in submitted.constraints.candidates
                       if candidate.destination.target == "mac")
            self.assertEqual(mac.eligibility, placement.FixedExclusion("full gate runs on natedev"))
            routes = target_availability.MacRoutes((), {}, UTC)
            shown = memory_feed.step_runs_on(submitted.route, routes, "check", str(fixture.root), "sample")
            self.assertEqual(shown.text(), "natedev only")
            self.assertEqual(shown.reason, "full gate runs on natedev")
            waiter = records.WaitingStep(request.owner.pid, "", "", "check", str(fixture.root),
                request.since, GIB, records.NeedsMemory(), route=submitted.route)
            self.assertEqual(routes.line(waiter), "Mac: no, full gate runs on natedev")

    def test_mac_only_build_offers_the_mac(self) -> None:
        from scripts.build_queue import placement

        real_socket = socket.socket
        with mock.patch("scripts.build_queue.admission_client.socket.socket", real_socket), \
             AdmissionFixture() as fixture, mock.patch.object(client, "CLOCK", fixture.clock):
            _ = subprocess.run(["git", "init", "-q", str(fixture.root)], capture_output=True, check=True)
            _ = (fixture.root / "Cargo.toml").write_text('[package]\nname="sample"\nversion="0.1.0"\n')
            _ = write_config_fixture(fixture.root / "mac_test.conf",
                f"offload=on\nlinux_only.{fixture.root.name}=\n", template="mac_test")
            _ = (fixture.root / "build_targets.conf").write_text(
                "targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
                + "kind.natedev=local\nplatform.natedev=linux\n")
            request = fixture.request(step="check")
            fixture.server.host_probe = lambda key: placement.HostEvidenceUnavailable(
                key, fixture.clock.now(), "the test has not published provider evidence")
            arguments = ["place-step", "--once", "--wait-events", "--mac-only", "--kind", "build",
                "--pid", str(request.owner.pid), "--episode", request.episode,
                "--since", str(request.since), "--repo-root", str(fixture.root), "--package", "sample",
                "--assignment-file", str(fixture.root / "assignment.json"), "--", "cargo", "build"]
            with fixture.serving(), \
                 mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                     request.profile, step_estimator.ReusableEstimate())), \
                 redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
                self.assertEqual(client.main(arguments), 78, errors.getvalue())
                with fixture.loop_turn():
                    constraints = fixture.server.placement_requests[request.episode].constraints
            self.assertIsInstance(constraints.selection, placement.MacOnlyHost)
            mac = next(candidate for candidate in constraints.candidates if candidate.destination.target == "mac")
            self.assertIsInstance(mac.eligibility, placement.AllowedHost)

    def test_a_mac_run_kind_reaches_the_service_with_its_command(self) -> None:
        from scripts.build_queue import placement

        real_socket = socket.socket
        for kind in placement.MAC_RUN_KINDS:
            with self.subTest(kind=kind), mock.patch("scripts.build_queue.admission_client.socket.socket", real_socket), \
                 AdmissionFixture() as fixture, mock.patch.object(client, "CLOCK", fixture.clock):
                _ = subprocess.run(["git", "init", "-q", str(fixture.root)], capture_output=True, check=True)
                _ = (fixture.root / "Cargo.toml").write_text('[package]\nname="sample"\nversion="0.1.0"\n')
                _ = write_config_fixture(fixture.root / "mac_test.conf",
                    f"offload=on\nlinux_only.{fixture.root.name}=\n", template="mac_test")
                _ = (fixture.root / "build_targets.conf").write_text(
                    "targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n")
                request = fixture.request(step="check")
                fixture.server.host_probe = lambda key: placement.HostEvidenceUnavailable(
                    key, fixture.clock.now(), "the test has not published provider evidence")
                arguments = ["place-step", "--once", "--wait-events", "--target", "mac", "--kind", kind,
                    "--pid", str(request.owner.pid), "--episode", request.episode,
                    "--since", str(request.since), "--repo-root", str(fixture.root), "--package", "sample",
                    "--assignment-file", str(fixture.root / "assignment.json"), "--", "/bin/zsh", "-c", "true"]
                with fixture.serving(), \
                     mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                         request.profile, step_estimator.ReusableEstimate())), \
                     redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
                    self.assertEqual(client.main(arguments), 78, errors.getvalue())
                    with fixture.loop_turn():
                        constraints = fixture.server.placement_requests[request.episode].constraints
                self.assertEqual(constraints.command_kind, kind)

    def test_unit_override_uses_the_shared_request_identity(self) -> None:
        with AdmissionFixture() as fixture:
            original = fixture.request()
            for unit, reason in (("queue-unit", ""), ("", ""),
                                 ("queue-unit", "it edits the merged checkout")):
                with self.subTest(unit=unit, reason=reason), mock.patch.dict(os.environ,
                        {records.BUILDLOG_UNIT: unit, "BUILD_QUEUE_LOCAL_REASON": reason}):
                    request = client.step_request(list(original.argv), original.cwd, original.owner,
                        original.since, original.sidecar, original.episode, original.profile)
                    self.assertEqual(request.identity.unit, unit)
                    self.assertEqual(request.route, target_config.LocalRequired(reason) if reason else records.DIRECT_CARGO)
                    launch = wire.LaunchRequest(request.episode, request.owner, request.since,
                                                request.identity, request.profile)
                    self.assertEqual(launch.identity.unit, unit)
                    self.assertEqual(records.waiter_identity("nextest", "tree").get("unit", ""), unit)

    def test_direct_step_identity_includes_the_terminal_worktree(self) -> None:
        # A terminal family gives no name, so the worktree folder names the caller.
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "",
                "CLAUDE_SESSION_NAME": "", "CLAUDE_SESSION_FOLDER": "", "BUILDLOG_OWNER_STAFF_ID": ""}), \
                mock.patch.object(records, "family_owner_name", return_value=""):
            identity = client.step_identity("nextest", "/work/queue-fixture")
        self.assertEqual(identity.name, "terminal · queue-fixture")

    def test_step_identity_names_each_caller_kind(self) -> None:
        with AdmissionFixture() as fixture, isolated_staff_file(fixture.root / "staff.json", {}):
            runner = fixture_member("showrunner", name="spacetime")
            unit = fixture_member("unit", name="navigation", parent=runner.id)
            worker = fixture_member("worker", name="impl", parent=unit.id)
            environment = {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_NAME": "",
                           "CLAUDE_SESSION_FOLDER": "", "BUILDLOG_UNIT": "", "BUILDLOG_OWNER_STAFF_ID": ""}
            for session, reference, folder, name, owner_unit in (
                (runner.current_session, "", "", "spacetime", ""),
                (unit.current_session, "", "", "navigation", "navigation"),
                (worker.current_session, "", "", "impl", "navigation"),
                ("", worker.id, "", "impl", "navigation"),
                ("unlisted", worker.id, "research", "research", ""),
                ("unlisted", "", "", "unregistered session", ""),
                ("", "", "", f"terminal · {Path.cwd().name}", ""),
            ):
                with self.subTest(session=session, reference=reference, folder=folder), mock.patch.dict(os.environ, {
                    **environment, "CLAUDE_CODE_SESSION_ID": session,
                    "CLAUDE_SESSION_FOLDER": folder, "BUILDLOG_OWNER_STAFF_ID": reference,
                }), mock.patch.object(records, "family_owner_name", return_value=""):
                    identity = client.step_identity("nextest", "")
                    self.assertEqual((identity.name, identity.unit), (name, owner_unit))
                    self.assertEqual(identity.session_id, worker.current_session if reference and not session else session)

    def use_transport(self, fixture: AdmissionFixture, *exchanges: Exchange) -> ScriptedTransport:
        transport = ScriptedTransport(fixture.clock, *exchanges)
        _ = self.enterContext(mock.patch.object(client, "CLOCK", fixture.clock))
        _ = self.enterContext(mock.patch("scripts.build_queue.admission_client.socket.socket", side_effect=transport.socket))
        _ = self.enterContext(mock.patch.object(client, "DefaultSelector", side_effect=transport.selector))
        return transport

    def use_socket_pairs(self, fixture: AdmissionFixture, *replies: wire.Reply | None,
                         endpoints: list[socket.socket] | None = None
                         ) -> tuple[list[socket.socket], list[wire.Request], mock.MagicMock]:
        pairs = [socket.socketpair() for _ in replies]
        if endpoints is not None:
            endpoints.extend(endpoint for endpoint, _peer in pairs)
        for endpoint, peer in pairs:
            _ = fixture.enter_context(endpoint)
            _ = fixture.enter_context(peer)
            endpoint.setblocking(False)
            peer.setblocking(False)
        received: list[wire.Request] = []
        read: set[socket.socket] = set()

        def answer_requests() -> None:
            for (_endpoint, peer), reply in zip(pairs, replies, strict=True):
                if peer in read:
                    continue
                try:
                    data = peer.recv(65536)
                except BlockingIOError:
                    continue
                if data:
                    received.append(wire.parse_request(data))
                    read.add(peer)
                    if reply is not None:
                        peer.sendall(reply_bytes(reply))

        _ = fixture.enter_context(mock.patch.object(client, "CLOCK", fixture.clock))
        _ = fixture.enter_context(mock.patch.object(socket.socket, "connect_ex", return_value=0))
        dial = fixture.enter_context(mock.patch.object(
            client, "_acquisition_socket", side_effect=[endpoint for endpoint, _peer in pairs]))
        _ = fixture.enter_context(mock.patch.object(client, "DefaultSelector", side_effect=partial(
            PrivateSocketSelector, fixture, before_read=answer_requests)))
        return [peer for _endpoint, peer in pairs], received, dial

    def test_priority_commands_from_any_session_send_the_actor(self) -> None:
        for claude, codex, actor in (("ordinary-session", "codex-session", "ordinary-session"),
                                      ("", "codex-session", "codex-session"), ("", "", "user")):
            for argv in (("priority-set", "unit"), ("priority-clear",)):
                with self.subTest(actor=actor, argv=argv), \
                     mock.patch.dict(os.environ), \
                     mock.patch.object(client, "_mutation", return_value=wire.Ok()) as mutation, \
                     redirect_stdout(io.StringIO()):
                    for key in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
                        _ = os.environ.pop(key, None)
                    if claude:
                        os.environ["CLAUDE_CODE_SESSION_ID"] = claude
                    if codex:
                        os.environ["CODEX_THREAD_ID"] = codex
                    self.assertEqual(client.main(list(argv)), 0)
                    mutation.assert_called_once()
                    request = cast(wire.PrioritySetRequest | wire.PriorityClearRequest,
                                   mutation.call_args.args[0])
                    self.assertIsInstance(request, (wire.PrioritySetRequest, wire.PriorityClearRequest))
                    self.assertEqual(request.actor, actor)

    def test_unusable_config_exits_with_checker_failure_and_names_the_key(self) -> None:
        with AdmissionFixture() as fixture:
            original = fixture.request()
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, estimate_refresh_seconds="invalid")
            with fixture.serving(), redirect_stderr(io.StringIO()) as errors:
                status = shell_place.main(["admit", "--pid", str(original.owner.pid), "--", "cargo", "nextest"])
            self.assertEqual(status, 2)
            self.assertIn(str(config), errors.getvalue())
            self.assertIn("estimate_refresh_seconds", errors.getvalue())
            self.assertEqual(len(errors.getvalue().splitlines()), 1)
            self.assertFalse(fixture.server.reservations)


    def test_recorded_queue_replies_pass_the_client_query_paths(self) -> None:
        with AdmissionFixture() as fixture, QueueFixture() as queue:
            reading_reply = queue.read()
            view_reply = reply_bytes(wire.ViewReply(reading_reply))
            explain_reply = reply_bytes(wire.ExplainReply(reading_reply.explanation))
            transport = self.use_transport(fixture, Exchange(view_reply), Exchange(explain_reply))
            reading = client.view()
            explanation = client.explain()
            self.assertTrue(reading.view.running)
            self.assertTrue(explanation.running)
            self.assertGreater(explanation.totals.measured, 0)
            self.assertEqual(transport.requests, [wire.ViewRequest(code_version=client.LOADED_ADMISSION_CODE_VERSION),
                                                  wire.ExplainRequest(code_version=client.LOADED_ADMISSION_CODE_VERSION)])

    def test_recorded_pressure_snapshot_waits_then_admits_without_inventing_condition(self) -> None:
        pressure = records.MemoryPressure(8, 15, 80)
        captured = wait_reply(pressure)
        captured = replace(captured, snapshot=replace(captured.snapshot,
            running=tuple(records.RunningStep("fixture", "worker", "check", "/tree", use,
                records.MeasuredMemory(use), pid=index) for index, use in enumerate((4 * GIB, GIB), 1)),
            decision=replace(captured.snapshot.decision, pressure=pressure)))
        with AdmissionFixture() as fixture:
            request = fixture.request()
            transport = self.use_transport(fixture, Exchange(reply_bytes(captured), TimedChunk(0.01, reply_bytes(GRANTED))))
            waits: list[wire.Wait] = []
            result = client.admit(request, waits.append, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertEqual(transport.requests, [request])
            self.assertEqual(len(waits), 1)
            pressure = waits[0].snapshot.decision.pressure
            assert isinstance(pressure, records.MemoryPressure)
            self.assertIsInstance(pressure.condition, records.PressureConditionNotRecorded)
            self.assertEqual((pressure.full_avg10, pressure.some_avg10, pressure.swap_used_percent), (8, 15, 80))
            self.assertEqual(waits[0].reason, pressure)
            self.assertEqual([step.measured.bytes for step in waits[0].snapshot.running
                              if isinstance(step.measured, records.MeasuredMemory)], [4 * GIB, GIB])
            self.assertEqual(records.pressure_text(pressure),
                             "memory pressure: condition not recorded by service; full avg10 8.0; "
                             + "some avg10 15.0; swap 80.0% used")



    def test_hold_after_grant_releases_before_waiting_and_uses_a_new_episode(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            final = replace(GRANTED, reservation="second")
            transport = self.use_transport(fixture, Exchange(reply_bytes(GRANTED)),
                Exchange(reply_bytes(wire.Ok())), Exchange(reply_bytes(final)))
            events: list[tuple[str, int]] = []
            checks = iter((True, False, True, True))
            def wait_until_free() -> None:
                events.append(("blocking wait", len(transport.attempts)))
            def admits() -> bool:
                attempts = len(transport.attempts)
                events.append(("hold check", attempts))
                if attempts == 2:
                    released = transport.requests[-1]
                    self.assertIsInstance(released, wire.ReleaseRequest)
                    assert isinstance(released, wire.ReleaseRequest)
                    self.assertEqual(released.reservation, GRANTED.reservation)
                return next(checks)
            hold = client.AdmissionHoldCheck(wait_until_free, admits)
            result = client.admit(request, lambda _reply: None, lambda: request, _attempt=hold)
            self.assertEqual(result, final)
            self.assertEqual(events, [("hold check", 0), ("hold check", 1), ("hold check", 2), ("hold check", 3)])
            first, release, second = transport.requests
            assert isinstance(first, wire.AdmitRequest)
            assert isinstance(release, wire.ReleaseRequest)
            assert isinstance(second, wire.AdmitRequest)
            self.assertEqual(release.reservation, GRANTED.reservation)
            self.assertNotEqual(first.episode, second.episode)
            self.assertEqual((first.owner, first.since, first.profile),
                             (second.owner, second.since, second.profile))

    def test_program_request_uses_its_checkout_instead_of_the_launcher_directory(self) -> None:
        with AdmissionFixture() as fixture:
            original = fixture.request()
            checkout = str(fixture.root / "other-checkout")
            profile = records.ProgramProfile("gate", "repo", checkout, records.HistoryPeakSizing(6 * GIB, records.FirstProgramPeak(6 * GIB), fixture.clock.now()))
            with mock.patch.object(step_estimator, "git_identity", side_effect=AssertionError("program inferred cargo")), \
                 mock.patch.object(client, "CLOCK", fixture.clock):
                request = client.step_request([], str(fixture.root), original.owner, original.since,
                                              "", original.episode, profile)
            self.assertEqual(request.identity.folder, checkout)
            self.assertEqual(request.profile, profile)
            self.assertEqual(request.identity.step, "gate")
            self.assertEqual(request.profile.peak, records.ExpectedPeak(6 * GIB, "fallback", 0))

    def test_failed_grant_file_write_releases_the_reservation_before_refusing(self) -> None:
        from pathlib import Path
        with AdmissionFixture() as fixture:
            original = fixture.request()
            shell_state = fixture.root / "grant.sh"
            _ = self.use_transport(fixture, Exchange(reply_bytes(GRANTED)), Exchange(reply_bytes(wire.Ok())))
            released: list[str] = []
            release = client.release
            def observe_release(reservation: str, status: int, configuration: records.MemoryConfig | wire.CleanupTiming) -> wire.Ok | wire.Refused:
                released.append(reservation)
                return release(reservation, status, configuration)
            with mock.patch.object(client, "step_request", return_value=original), \
                 mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                 mock.patch.object(client, "write_shell_grant", side_effect=OSError("grant disk failed")), \
                 mock.patch.object(client, "release", side_effect=observe_release), \
                 redirect_stderr(io.StringIO()), mock.patch("sys.stdout", io.StringIO()) as output:
                status = shell_place.main(["admit", "--pid", str(original.owner.pid),
                                      "--shell-state", str(shell_state), "--", "cargo", "check"])
            self.assertEqual(status, 2)
            self.assertEqual(released, [GRANTED.reservation])
            self.assertEqual(output.getvalue(), "")
            self.assertIn("BUILDLOG_MEM_OUTCOME=Refused", Path(shell_state).read_text())
            self.assertNotIn("BUILDLOG_MEM_OUTCOME=Granted", shell_state.read_text())

    def test_cli_refresh_uses_step_estimate_and_keeps_the_episode_and_first_since(self) -> None:
        for caller in ("verify.sh", "ci_cargo_front.py"):
            with self.subTest(caller=caller), AdmissionFixture() as fixture:
                original = fixture.request()
                runner_request = fixture.service_request(owner=original.owner)
                updated = replace(original.profile, estimated_at=fixture.clock.now() + 1)
                transport = self.use_transport(fixture, Exchange(reply_bytes(EXPIRED), after_send={2: (reply_bytes(GRANTED),)}))
                estimates = [step_estimator.StepEstimate(original.profile, records.FreshTest()),
                             step_estimator.StepEstimate(updated, step_estimator.ReusableEstimate())]
                with mock.patch.object(step_estimator, "step_estimate", side_effect=estimates) as estimate, \
                     mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                     redirect_stderr(io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
                    if caller == "verify.sh":
                        status = shell_place.main(["admit", "--pid", str(original.owner.pid), "--since", str(original.since),
                                                  "--", "cargo", "nextest", "run"])
                        self.assertEqual(status, 0)
                        self.assertEqual(estimate.call_count, 2)
                    else:
                        # The CI front uses this path through held_reservation.
                        self.assertEqual(shell_place.request_place(runner_request, client.WaitOutput()), GRANTED)
                        estimate.assert_not_called()
                self.assertEqual(len(transport.attempts), 1)
                first, second = transport.requests
                if caller == "verify.sh":
                    assert isinstance(first, wire.AdmitRequest)
                    assert isinstance(second, wire.AdmitRequest)
                    self.assertEqual((first.episode, first.since, first.owner), (second.episode, second.since, second.owner))
                    self.assertEqual(first.since, original.since)
                    self.assertTrue(first.fresh_test)
                    self.assertFalse(second.fresh_test)
                    self.assertEqual(second.profile, updated)
                else:
                    assert isinstance(first, wire.ServiceEstimateRequest)
                    self.assertEqual(first, runner_request)
                    self.assertEqual(second, first)
                self.doCleanups()

    def test_build_generation_precedes_history_and_survives_transport_reconnect(self) -> None:
        for expired_cache in (False, True):
            with self.subTest(expired_cache=expired_cache), AdmissionFixture() as fixture:
                owner = fixture.request()
                profile: records.InferFromArgv | wire.Profile = records.INFER_FROM_ARGV
                if expired_cache:
                    assert isinstance(owner.profile, records.StepProfile)
                    profile = replace(owner.profile, step="build", build_history_generation=records.BuildHistoryGeneration(6),
                        estimated_at=fixture.clock.now() - records.read_memory_config().policy.timing.estimate_refresh_seconds - 1)
                transport = self.use_transport(fixture, Exchange(reply_bytes(wire.BuildHistoryReply(7))),
                    Exchange(reply_bytes(WAIT), b""), Exchange(reply_bytes(GRANTED)))
                def history_peak(*_args: object, **_kwargs: object) -> records.ExpectedPeak:
                    self.assertIsInstance(transport.requests[-1], wire.BuildHistoryRequest)
                    return records.ExpectedPeak(12 * GIB, "fallback", 0)
                with mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), str(fixture.root))), \
                        mock.patch.object(step_estimator, "expected_peak", side_effect=history_peak) as history:
                    with mock.patch.object(conf, "read", wraps=conf.read) as reader:
                        request = client.step_request(["cargo", "build"], str(fixture.root), owner.owner,
                            owner.since, "", owner.episode, profile)
                    reader.assert_called_once_with("build_admission")
                    self.assertEqual(client.admit(request, lambda _wait: None, lambda: request), GRANTED)
                self.assertEqual(history.call_count, 1)
                self.assertEqual(sum(isinstance(item, wire.BuildHistoryRequest) for item in transport.requests), 1)
                first, second = [item for item in transport.requests if isinstance(item, wire.AdmitRequest)]
                self.assertEqual(first, second)
                assert isinstance(second.profile, records.StepProfile)
                self.assertEqual(second.profile.build_history_generation, records.BuildHistoryGeneration(7))
                self.doCleanups()

    def test_cached_build_estimate_keeps_its_original_history_generation(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="build")
            with mock.patch.object(client, "CLOCK", fixture.clock), \
                    mock.patch.object(client, "build_history_generation", side_effect=AssertionError("cached estimate queried a newer generation")), \
                    mock.patch.object(step_estimator, "expected_peak", side_effect=AssertionError("cached estimate read history")):
                retained = client.step_request(list(request.argv), request.cwd, request.owner,
                    request.since, request.sidecar, request.episode, request.profile)
            self.assertEqual(retained.profile, request.profile)
            self.assertEqual((retained.owner, retained.episode, retained.since),
                (request.owner, request.episode, request.since))

    def test_expired_build_reads_the_current_generation_before_refreshing_on_its_connection(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="build")
            granted = replace(GRANTED, need=16 * GIB)
            transport = self.use_transport(fixture,
                Exchange(reply_bytes(EXPIRED), after_send={2: (reply_bytes(wire.BuildHistoryReply(1)),), 3: (reply_bytes(granted),)}))
            def history_peak(*_args: object, **_kwargs: object) -> records.ExpectedPeak:
                self.assertIsInstance(transport.requests[-1], wire.BuildHistoryRequest)
                return records.ExpectedPeak(16 * GIB, "fallback", 0)
            def refresh() -> wire.AdmitRequest:
                return client.step_request(list(request.argv), request.cwd, request.owner,
                    request.since, request.sidecar, request.episode)
            with mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), str(fixture.root))), \
                    mock.patch.object(step_estimator, "expected_peak", side_effect=history_peak) as history:
                self.assertEqual(client.admit(request, lambda _wait: None, refresh), granted)
            self.assertEqual(history.call_count, 1)
            self.assertEqual(len(transport.attempts), 1)
            first, generation, refreshed = transport.requests
            self.assertIsInstance(generation, wire.BuildHistoryRequest)
            assert isinstance(first, wire.AdmitRequest) and isinstance(refreshed, wire.AdmitRequest)
            self.assertEqual((first.owner, first.episode, first.since),
                (refreshed.owner, refreshed.episode, refreshed.since))
            assert isinstance(refreshed.profile, records.StepProfile)
            self.assertEqual(refreshed.profile.build_history_generation, records.BuildHistoryGeneration(1))
            self.assertEqual(refreshed.profile.peak.bytes, granted.need)

    def test_expired_build_retries_after_an_estimator_io_error(self) -> None:
        with AdmissionFixture(skip_idle_spacing=True) as fixture:
            fixture.configure(reconnect_seconds=1)
            seed = fixture.request(step="build", since=fixture.clock.now() - 60)
            argv = ["cargo", "build", "-p", "cargo-mend", "--bin", "cargo-mend"]
            estimates = 0
            admissions: list[wire.AdmitRequest] = []
            expired_orders: list[float] = []
            handle = fixture.server.handle_request
            read_line = cast(Callable[[socket.socket, bytearray, float], bytes], getattr(client, "_line"))
            exchange = LauncherHangGuard("Estimator retry replies")

            def receive_reply(connection: socket.socket, pending: bytearray, deadline: float) -> bytes:
                while b"\n" not in pending:
                    if not select.select([connection], [], [], 0)[0]:
                        try:
                            remaining = exchange.remaining()
                        except TimeoutError as error:
                            raise AssertionError("the queue did not send the estimator retry reply") from error
                        fixture.server.run_once(remaining)
                        continue
                    chunk = connection.recv(65536)
                    if not chunk:
                        raise client.TransportUnavailable("closed connection")
                    pending.extend(chunk)
                return read_line(connection, pending, deadline)

            def estimate(*_args: object, **_kwargs: object) -> records.ExpectedPeak:
                nonlocal estimates
                estimates += 1
                if estimates == 1:
                    with fixture.loop_turn():
                        fixture.server.build_history_generation += 1
                elif estimates == 2:
                    raise OSError(errno.ESTALE, "test estimate read failed")
                return records.ExpectedPeak(2 * GIB, "fallback", 0)

            def observe(request: wire.Request,
                        connection: service.Connection | service.NoConnection) -> list[wire.Reply]:
                if isinstance(request, wire.AdmitRequest):
                    admissions.append(request)
                return handle(request, connection)

            def record_expiry_order(reply: wire.Reply) -> wire.Reply:
                if isinstance(reply, wire.Wait):
                    cause = reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason
                    if isinstance(cause, records.EstimateExpired):
                        row = fixture.server.waiters[seed.episode].row
                        expired_orders.append(row.get("order_since", row["since"]))
                return reply

            def refresh() -> wire.AdmitRequest:
                return client.step_request(argv, seed.cwd, seed.owner, seed.since, "", seed.episode)

            with mock.patch.object(client, "CLOCK", fixture.clock), \
                    mock.patch.object(client, "DefaultSelector", side_effect=partial(PrivateSocketSelector, fixture)), \
                    mock.patch.object(fixture.server, "handle_request", side_effect=observe), \
                    mock.patch.object(fixture.server, "reply_filter", side_effect=record_expiry_order), \
                    mock.patch.object(client, "_line", new=receive_reply), \
                    mock.patch.object(step_estimator, "git_identity",
                                      return_value=(records.UnknownRepository(), "tree")), \
                    mock.patch.object(step_estimator, "expected_peak", side_effect=estimate), exchange.protect():
                reply = shell_place.request_place(shell_place.RefreshedRequest(refresh), client.WaitOutput())
            self.assertIsInstance(reply, wire.Admitted)
            self.assertEqual(estimates, 3)
            self.assertEqual({request.episode for request in admissions}, {seed.episode})
            self.assertEqual({request.since for request in admissions}, {seed.since})
            self.assertEqual(set(expired_orders), {seed.since})
            row = fixture.server.reservations[seed.episode].queue_waiter
            self.assertEqual(row.get("order_since"), seed.since)
            assert isinstance(admissions[-1].profile, records.StepProfile)
            self.assertEqual(admissions[-1].profile.build_history_generation, records.BuildHistoryGeneration(1))

    def test_expired_estimate_retries_after_disconnect_during_refresh(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(reconnect_seconds=3, unreachable_limit_seconds=5)
            request = fixture.request(since=fixture.clock.now() - 60)
            updated = replace(request, episode="different-episode", since=fixture.clock.now(),
                              profile=replace(request.profile, estimated_at=fixture.clock.now()))
            transport = self.use_transport(fixture, Exchange(reply_bytes(EXPIRED)),
                                           Exchange(reply_bytes(EXPIRED), after_send={2: (reply_bytes(GRANTED),)}))
            send = ScriptSocket.sendall

            def disconnect_on_refresh(connection: ScriptSocket, data: bytes) -> None:
                if len(transport.attempts) == 1 and transport.requests:
                    raise BrokenPipeError(errno.EPIPE, "the service disconnected during the refresh")
                send(connection, data)

            with mock.patch.object(ScriptSocket, "sendall", autospec=True, side_effect=disconnect_on_refresh):
                refresh = mock.Mock(return_value=updated)
                self.assertEqual(client.admit(request, lambda _wait: None, refresh), GRANTED)
            self.assertEqual(refresh.call_count, 2)
            self.assertEqual(transport.requests, [request, request, replace(request, profile=updated.profile)])

    def test_repeated_refresh_disconnects_stop_at_the_unreachable_limit(self) -> None:
        for read_history in (False, True):
            with self.subTest(read_history=read_history), AdmissionFixture() as fixture:
                fixture.configure(reconnect_seconds=3, unreachable_limit_seconds=5)
                request = fixture.request(since=fixture.clock.now() - 60)
                refreshed = replace(request, profile=replace(request.profile, estimated_at=fixture.clock.now() + 1))
                history = (reply_bytes(wire.BuildHistoryReply(1)),) if read_history else ()
                transport = self.use_transport(fixture, *(Exchange(reply_bytes(EXPIRED), after_send={2: history}) for _ in range(4)),
                    Exchange(reply_bytes(wire.Refused("refresh retry exceeded the fixture limit"))))
                send = ScriptSocket.sendall

                def close_before_refresh(connection: ScriptSocket, data: bytes) -> None:
                    if wire.parse_request(data) == refreshed:
                        raise BrokenPipeError(errno.EPIPE, "closed before the refreshed estimate")
                    send(connection, data)

                def refresh() -> wire.AdmitRequest:
                    if read_history:
                        self.assertEqual(client.build_history_generation(), records.BuildHistoryGeneration(1))
                    return refreshed

                with mock.patch.object(ScriptSocket, "sendall", autospec=True, side_effect=close_before_refresh), \
                        redirect_stderr(io.StringIO()):
                    result = client.admit(request, lambda _wait: None, refresh)
                self.assertEqual(result, wire.Refused(
                    "build queue unreachable for 5 s; step stopped; cargo token given back"))
                self.assertEqual(transport.attempts, [0, 3])
                self.assertEqual(transport.selector_timeouts, [3, 2])
                self.assertEqual(fixture.clock.sleeps, [])
                self.assertEqual(fixture.clock.monotonic(), 5)
                self.assertEqual([item for item in transport.requests if isinstance(item, wire.AdmitRequest)],
                                 [request, request])
                self.doCleanups()

    def test_generation_outage_after_expiry_keeps_the_leave_hook_time_and_cli_status(self) -> None:
        for failure in (b"", b"invalid generation reply\n"):
            with self.subTest(failure=failure), AdmissionFixture() as fixture:
                fixture.configure(running_pass_seconds=5, reconnect_seconds=2)
                interval = records.read_memory_config().running_pass_seconds
                original = fixture.request()
                transport = self.use_transport(fixture,
                    Exchange(reply_bytes(wire.BuildHistoryReply(0))),
                    Exchange(reply_bytes(WAIT), TimedChunk(interval - 2, reply_bytes(EXPIRED)), after_send={2: (failure,)}),
                    *(Exchange(connect_error=ConnectionRefusedError()) for _ in range(6)),
                    Exchange(reply_bytes(EXPIRED), reply_bytes(wire.BuildHistoryReply(1))),
                    Exchange(reply_bytes(GRANTED)))
                hook_times: list[float] = []

                def leave() -> bool:
                    hook_times.append(fixture.clock.monotonic())
                    return True

                with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "test-leave"}), \
                        mock.patch.object(client, "_leave_queue", side_effect=leave), \
                        mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                        mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), str(fixture.root))), \
                        mock.patch.object(step_estimator, "expected_peak", return_value=records.ExpectedPeak(12 * GIB, "fallback", 0)), \
                        redirect_stderr(io.StringIO()) as errors, mock.patch("sys.stdout", io.StringIO()) as output:
                    status = shell_place.main(["admit", "--pid", str(original.owner.pid), "--", "cargo", "build"])
                self.assertEqual(status, 75, errors.getvalue())
                self.assertEqual(hook_times, [interval], "generation retries must keep the original hook time")
                self.assertEqual(fixture.clock.monotonic(), interval)
                self.assertEqual(output.getvalue(), "")
                self.assertNotIn("step refused", errors.getvalue())
                self.assertGreaterEqual(sum(isinstance(item, wire.BuildHistoryRequest)
                    for item in transport.requests), 2)
                admissions = [item for item in transport.requests if isinstance(item, wire.AdmitRequest)]
                self.assertTrue(admissions)
                self.assertTrue(all((item.owner, item.episode, item.since) ==
                    (admissions[0].owner, admissions[0].episode, admissions[0].since) for item in admissions))
                self.doCleanups()

    def test_unreachable_generation_refresh_stops_the_cli_without_checker_retries(self) -> None:
        for command in ("admit", "launch"):
            for generation_failure in (b"", b"invalid generation reply\n"):
                with self.subTest(command=command, failure=generation_failure), AdmissionFixture() as fixture:
                    fixture.configure(reconnect_seconds=3, unreachable_limit_seconds=5)
                    original = fixture.request(step="build")
                    transport = self.use_transport(fixture,
                        Exchange(reply_bytes(wire.BuildHistoryReply(0))),
                        Exchange(reply_bytes(EXPIRED), after_send={2: (generation_failure,)}),
                        *(Exchange(connect_error=FileNotFoundError()) for _ in range(3)))
                    with mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                            mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), str(fixture.root))), \
                            mock.patch.object(step_estimator, "expected_peak", return_value=records.ExpectedPeak(12 * GIB, "fallback", 0)), \
                            redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
                        status = shell_place.main([command, "--pid", str(original.owner.pid), "--", "cargo", "build"])
                    self.assertEqual(status, 2)
                    self.assertEqual(fixture.clock.monotonic(), 5 if not generation_failure else 8)
                    self.assertEqual(sum(isinstance(request, wire.BuildHistoryRequest)
                                         for request in transport.requests), 2)
                    self.assertEqual(errors.getvalue().count("build queue not reachable; trying for 5 s"), 1)
                    self.assertEqual(errors.getvalue().count(
                        "build queue unreachable for 5 s; step stopped; cargo token given back"), 1)
                    self.assertNotIn("queue reached again", errors.getvalue())
                    self.assertNotIn("step refused", errors.getvalue())
                    self.doCleanups()

    def test_generation_retries_cannot_extend_the_original_launch_deadline(self) -> None:
        for stage in ("initial", "refresh"):
            for failure in ("connect", "silent"):
                with self.subTest(stage=stage, failure=failure), AdmissionFixture() as fixture:
                    fixture.configure(reconnect_seconds=2)
                    original = fixture.request()
                    exchanges: list[Exchange] = [] if stage == "initial" else [
                        Exchange(reply_bytes(wire.BuildHistoryReply(0))),
                        Exchange(TimedChunk(3, reply_bytes(EXPIRED)), b"invalid generation reply\n")]
                    exchanges.extend(Exchange(connect_error=ConnectionRefusedError()) if failure == "connect"
                        else Exchange(TimeoutError()) for _ in range(12))
                    transport = self.use_transport(fixture, *exchanges)
                    with mock.patch.dict(os.environ, {"BUILDLOG_MEM_WAIT_LIMIT_S": "6", "LINT_QUEUE_LEAVE_HOOK": "", "BUILDLOG_MAC_IDLE_HOOK": ""}), \
                            mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                            mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), str(fixture.root))), \
                            mock.patch.object(step_estimator, "expected_peak", return_value=records.ExpectedPeak(12 * GIB, "fallback", 0)), \
                            redirect_stderr(io.StringIO()) as errors, mock.patch("sys.stdout", io.StringIO()) as output:
                        status = shell_place.main(["launch", "--pid", str(original.owner.pid), "--", "cargo", "build"])
                    self.assertEqual(status, 2)
                    self.assertIn("launch wait limit reached without admission", errors.getvalue())
                    self.assertEqual(fixture.clock.monotonic(), 6, "generation retries must use the original launch deadline")
                    self.assertTrue(all(attempt < 6 for attempt in transport.attempts))
                    self.assertEqual(output.getvalue(), "")
                    self.doCleanups()

    def test_grant_sent_during_a_slow_leave_hook_is_read_before_the_next_hook(self) -> None:
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", running_pass_seconds="5")
            interval = records.read_memory_config().running_pass_seconds
            request = fixture.request()
            exchange = Exchange(reply_bytes(WAIT), TimeoutError())
            transport = self.use_transport(fixture, exchange)
            hooks: list[float] = []

            def decline_after_grant() -> bool:
                hooks.append(fixture.clock.monotonic())
                self.assertEqual(len(hooks), 1, "the client must read the grant before another hook")
                fixture.clock.advance(interval)
                exchange.events.append(reply_bytes(GRANTED))
                fixture.clock.advance(interval)
                return False

            with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "slow-decline"}), \
                    mock.patch.object(client, "_leave_queue", side_effect=decline_after_grant):
                result = client.admit(request, lambda _reply: None, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertEqual(hooks, [interval])
            self.assertEqual(transport.requests, [request])
            self.assertEqual(exchange.events, [])

    def test_split_grant_survives_a_declined_leave_hook(self) -> None:
        for kind, partial_at_deadline in (("admit", False), ("launch", False), ("admit", True), ("launch", True)):
            with self.subTest(kind=kind, partial_at_deadline=partial_at_deadline), AdmissionFixture() as fixture:
                write_memory_config(fixture.root / "build_admission.conf", running_pass_seconds="5")
                interval = records.read_memory_config().running_pass_seconds
                request = fixture.request() if kind == "admit" else fixture.launch()
                grant = GRANTED if kind == "admit" else wire.LaunchAdmitted(GIB, "new-source")
                encoded = reply_bytes(grant)
                split = len(encoded) // 2
                transport = self.use_transport(fixture, Exchange(reply_bytes(WAIT),
                    TimedChunk(interval if partial_at_deadline else interval - 1, encoded[:split]), TimeoutError(),
                    TimedChunk(1, encoded[split:])))
                hooks: list[float] = []
                waits: list[wire.Wait] = []

                def decline() -> bool:
                    hooks.append(fixture.clock.monotonic())
                    return False

                with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "test-decline"}), \
                        mock.patch.object(client, "_leave_queue", side_effect=decline), \
                        redirect_stderr(io.StringIO()) as errors:
                    if isinstance(request, wire.AdmitRequest):
                        admit_request = request
                        result = client.admit(admit_request, waits.append, lambda: admit_request)
                    else:
                        launch_request = request
                        result = client.launch(launch_request, waits.append, lambda: launch_request)
                self.assertEqual(result, grant)
                self.assertEqual(hooks, [interval, 2 * interval] if partial_at_deadline else [interval])
                self.assertEqual(waits, [WAIT])
                self.assertEqual(transport.requests, [request])
                self.assertEqual(len(transport.attempts), 1)
                self.assertEqual(fixture.clock.monotonic(), (2 * interval if partial_at_deadline else interval) + 1)
                self.assertEqual(errors.getvalue(), "")
                self.doCleanups()

    def test_split_reply_survives_a_silence_timeout(self) -> None:
        mutations: dict[str, Callable[[], object]] = {
            "release": lambda: client.release("stem", 137, records.read_memory_config()),
            "width": lambda: client.width("stem", 3),
            "priority-set": lambda: client.priority_set("unit", "urgent", "director"),
            "priority-clear": lambda: client.priority_clear("director"),
            "priority-clear-unit": lambda: client.priority_clear("director", "demo"),
            "token-wait": lambda: client.token_wait("stem", records.read_memory_config())}
        for kind in ("admit", "launch", *mutations):
            with self.subTest(kind=kind), AdmissionFixture() as fixture:
                write_memory_config(fixture.root / "build_admission.conf", silence_seconds="7")
                interval = records.read_memory_config().silence_seconds
                request = fixture.request() if kind == "admit" else fixture.launch()
                reacquires = kind in ("admit", "launch")
                if reacquires:
                    expected, replay = self.saved_grant_replay(fixture, request)
                    encoded = reply_bytes(expected)
                    exchanges = (Exchange(encoded[:len(encoded) // 2], TimeoutError()), replay)
                else:
                    expected = wire.Ok()
                    encoded = reply_bytes(expected)
                    split = len(encoded) // 2
                    exchanges = (Exchange(encoded[:split], TimeoutError(), encoded[split:]),)
                transport = self.use_transport(fixture, *exchanges)
                with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "", "BUILDLOG_MAC_IDLE_HOOK": ""}), \
                        redirect_stderr(io.StringIO()) as errors:
                    if kind == "admit":
                        assert isinstance(request, wire.AdmitRequest)
                        admit_request = request
                        result = client.admit(admit_request, lambda _wait: None, lambda: admit_request)
                    elif kind == "launch":
                        assert isinstance(request, wire.LaunchRequest)
                        launch_request = request
                        result = client.launch(launch_request, lambda _wait: None, lambda: launch_request)
                    else:
                        result = mutations[kind]()
                self.assertEqual(result, expected)
                self.assertEqual(fixture.clock.monotonic(), interval)
                if reacquires:
                    self.assertEqual(transport.requests, [request, request])
                    self.assertEqual(transport.attempts, [0, interval])
                else:
                    self.assertEqual(len(transport.requests), 1)
                    self.assertEqual(len(transport.attempts), 1)
                self.assertEqual(errors.getvalue().count("admission service not answering since"), 1)
                self.assertNotIn("check failed", errors.getvalue())
                self.doCleanups()

    def test_cleanup_uses_acquired_silence_after_configuration_becomes_unusable(self) -> None:
        for kind in ("release", "token-wait"):
            with self.subTest(kind=kind), AdmissionFixture() as fixture:
                config = fixture.root / "build_admission.conf"
                write_memory_config(config, silence_seconds="7")
                acquired = records.read_memory_config()
                write_memory_config(config, silence_seconds="invalid")
                encoded = reply_bytes(wire.Ok())
                split = len(encoded) // 2
                transport = self.use_transport(fixture,
                    Exchange(encoded[:split], TimeoutError(), encoded[split:]))
                with mock.patch.object(records, "read_memory_config", side_effect=AssertionError("cleanup read config")), \
                        redirect_stderr(io.StringIO()) as errors:
                    result = client.release("stem", 137, acquired) if kind == "release" else client.token_wait("stem", acquired)
                self.assertEqual(result, wire.Ok())
                self.assertEqual(fixture.clock.monotonic(), 7)
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(len(transport.attempts), 1)
                self.assertEqual(errors.getvalue().count("admission service not answering since"), 1)
                self.assertNotIn("check failed", errors.getvalue())
                self.doCleanups()

    def test_standalone_release_succeeds_after_configuration_is_corrupted(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            grant, = fixture.server.handle_request(request)
            assert isinstance(grant, wire.Admitted)
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, silence_seconds="invalid")
            with fixture.serving(), redirect_stderr(io.StringIO()) as errors, \
                    mock.patch("sys.stdout", io.StringIO()) as output:
                status = shell_place.main(["release", grant.reservation, "137"])
            self.assertEqual(status, 0)
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(wire.parse_record(output.getvalue(), wire.Ok), wire.Ok())
            self.assertFalse(fixture.server.reservations)

    def test_recovered_cargo_peak_warns_once_for_the_complete_step_request(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, **{"program_first_peak_gib.cargo": "7"})
            commit_config_fixture(config)
            write_memory_config(config, **{"program_first_peak_gib.cargo": "invalid"})
            original = fixture.request()
            repository = records.RepositoryIdentity("github.com/team/project", "project")
            with mock.patch.object(step_estimator, "git_identity", return_value=(repository, "tree")), \
                    mock.patch.object(conf, "read", wraps=conf.read) as reader, \
                    redirect_stderr(io.StringIO()) as errors:
                request = client.step_request(["cargo", "clippy"], str(fixture.root), original.owner,
                                              original.since, "", original.episode)
            reader.assert_called_once_with("build_admission")
            self.assertEqual(request.profile.peak.bytes, 7 * GIB)
            self.assertEqual(request.profile.peak.source, "fallback")
            self.assertEqual(errors.getvalue().count("program_first_peak_gib.cargo='invalid'"), 1)
            self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_recovered_key_with_an_unavailable_key_prints_only_the_refusal(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, estimate_refresh_seconds="321", **{"program_first_peak_gib.cargo": None})
            commit_config_fixture(config)
            write_memory_config(config, estimate_refresh_seconds="invalid", **{"program_first_peak_gib.cargo": "invalid"})
            original = fixture.request()
            with redirect_stderr(io.StringIO()) as errors:
                status = shell_place.main(["admit", "--pid", str(original.owner.pid), "--", "cargo", "clippy"])
            self.assertEqual(status, 2)
            self.assertIn(str(config), errors.getvalue())
            self.assertIn("program_first_peak_gib.cargo='invalid'", errors.getvalue())
            self.assertEqual(len(errors.getvalue().splitlines()), 1)
            self.assertNotIn("warning:", errors.getvalue())
            self.assertFalse(fixture.server.reservations)

    def test_split_generation_reply_survives_a_declined_leave_hook(self) -> None:
        for stage in ("initial", "refresh"):
            with self.subTest(stage=stage), AdmissionFixture() as fixture:
                write_memory_config(fixture.root / "build_admission.conf", running_pass_seconds="0.25")
                interval = records.read_memory_config().running_pass_seconds
                original = fixture.request(step="build")
                encoded = reply_bytes(wire.BuildHistoryReply(7))
                split = len(encoded) // 2
                history = (TimedChunk(interval / 2, encoded[:split]), TimeoutError(), TimedChunk(interval / 4, encoded[split:]))
                exchanges = ([Exchange(*history), Exchange(reply_bytes(GRANTED))] if stage == "initial"
                             else [Exchange(reply_bytes(EXPIRED), after_send={2: history, 3: (reply_bytes(GRANTED),)})])
                transport = self.use_transport(fixture, *exchanges)
                hooks: list[float] = []

                def decline() -> bool:
                    hooks.append(fixture.clock.monotonic())
                    return False

                def refresh() -> wire.AdmitRequest:
                    return client.step_request(list(original.argv), original.cwd, original.owner,
                        original.since, original.sidecar, original.episode)

                waits: list[wire.Wait] = []
                with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "test-decline"}), \
                        mock.patch.object(client, "_leave_queue", side_effect=decline), \
                        mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), original.cwd)), \
                        mock.patch.object(step_estimator, "expected_peak", return_value=records.ExpectedPeak(12 * GIB, "fallback", 0)) as peak, \
                        redirect_stderr(io.StringIO()) as errors:
                    request = refresh() if stage == "initial" else original
                    result = client.admit(request, waits.append, refresh)
                self.assertEqual(result, GRANTED)
                self.assertEqual(hooks, [interval])
                self.assertEqual(waits, [] if stage == "initial" else [EXPIRED])
                self.assertEqual(peak.call_count, 1)
                self.assertEqual(len(transport.attempts), 2 if stage == "initial" else 1)
                self.assertEqual(sum(isinstance(item, wire.BuildHistoryRequest) for item in transport.requests), 1)
                admissions = [item for item in transport.requests if isinstance(item, wire.AdmitRequest)]
                self.assertTrue(all((item.owner, item.episode, item.since) ==
                    (original.owner, original.episode, original.since) for item in admissions))
                self.assertEqual(len(admissions), 1 if stage == "initial" else 2)
                assert isinstance(admissions[-1].profile, records.StepProfile)
                self.assertEqual(admissions[-1].profile.build_history_generation, records.BuildHistoryGeneration(7))
                self.assertEqual(errors.getvalue(), "")
                self.doCleanups()

    def test_complete_reply_retains_the_partial_next_reply(self) -> None:
        for next_reply in ("grant", "generation"):
            with self.subTest(next_reply=next_reply), AdmissionFixture() as fixture:
                write_memory_config(fixture.root / "build_admission.conf", running_pass_seconds="0.25")
                interval = records.read_memory_config().running_pass_seconds
                request = fixture.request(step="build")
                first = WAIT if next_reply == "grant" else EXPIRED
                encoded = reply_bytes(GRANTED if next_reply == "grant" else wire.BuildHistoryReply(7))
                split = len(encoded) // 2
                after_send: dict[int, tuple[bytes | TimedChunk | Exception, ...]] | None = (
                    {3: (reply_bytes(GRANTED),)} if next_reply == "generation" else None)
                exchange = Exchange(reply_bytes(first) + encoded[:split], TimeoutError(), after_send=after_send)
                transport = self.use_transport(fixture, exchange)
                hooks: list[float] = []
                waits: list[wire.Wait] = []

                def decline() -> bool:
                    hooks.append(fixture.clock.monotonic())
                    exchange.events.append(encoded[split:])
                    return False

                def refresh() -> wire.AdmitRequest:
                    return client.step_request(list(request.argv), request.cwd, request.owner,
                        request.since, request.sidecar, request.episode)

                with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "test-decline"}), \
                        mock.patch.object(client, "_leave_queue", side_effect=decline), \
                        mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), request.cwd)), \
                        mock.patch.object(step_estimator, "expected_peak", return_value=records.ExpectedPeak(12 * GIB, "fallback", 0)), \
                        redirect_stderr(io.StringIO()) as errors:
                    result = client.admit(request, waits.append, refresh)
                self.assertEqual(result, GRANTED)
                self.assertEqual(waits, [first])
                self.assertEqual(hooks, [interval])
                self.assertEqual(len(transport.attempts), 1)
                self.assertEqual(sum(isinstance(item, wire.BuildHistoryRequest) for item in transport.requests),
                    0 if next_reply == "grant" else 1)
                self.assertEqual(errors.getvalue(), "")
                self.doCleanups()

    def test_reconnect_discards_a_partial_reply_from_the_old_socket(self) -> None:
        for kind in ("admit", "generation"):
            with self.subTest(kind=kind), AdmissionFixture() as fixture:
                request = fixture.request()
                expected = GRANTED if kind == "admit" else wire.BuildHistoryReply(7)
                encoded = reply_bytes(expected)
                old = Exchange(encoded[:len(encoded) // 2], TimeoutError(), b"")
                transport = self.use_transport(fixture, old, Exchange(encoded))
                with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "", "BUILDLOG_MAC_IDLE_HOOK": ""}), \
                        redirect_stderr(io.StringIO()) as errors:
                    result = client.admit(request, lambda _wait: None, lambda: request) if kind == "admit" \
                        else client.build_history_generation()
                self.assertEqual(result, GRANTED if kind == "admit" else records.BuildHistoryGeneration(7))
                self.assertEqual(len(transport.attempts), 2)
                self.assertEqual(len(transport.requests), 2)
                self.assertEqual(transport.requests[0], transport.requests[1])
                self.assertNotIn("check failed", errors.getvalue())
                self.doCleanups()

    def test_down_alert_sends_once_until_a_restart_runs_for_the_silence_interval(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = time.time()
            fixture.configure(silence_seconds=10, reconnect_seconds=1)
            commands: list[tuple[list[str], str]] = []

            def deliver(command: list[str], *, input: str, text: bool, capture_output: bool,
                        timeout: float) -> subprocess.CompletedProcess[str]:
                self.assertTrue(text)
                self.assertTrue(capture_output)
                self.assertGreater(timeout, 0)
                commands.append((command, input))
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(client.down_alert(), 0)
                self.assertRegex(output.getvalue(), r"^already claimed by [0-9a-f]+\n$")
                self.assertEqual(len(commands), sender.call_count)
                return subprocess.CompletedProcess(command, 0, "SENT: to builds\n", "")

            with mock.patch.object(client, "CLOCK", fixture.clock), \
                    mock.patch.object(client, "view", side_effect=wire.AdmissionUnavailable("no socket")), \
                    mock.patch.object(subprocess, "run", side_effect=deliver) as sender:
                with redirect_stdout(io.StringIO()) as sent_output:
                    self.assertEqual(client.down_alert(), 0)
                self.assertEqual(sent_output.getvalue(), "SENT: to builds\n")
                for attempt in range(3):
                    with self.subTest(restart=attempt), fixture.loop_turn():
                        fixture.restart()
                        fixture.clock.advance(9)
                        fixture.server.run_once()
                        self.assertIsInstance(outage_alerts.claim("build-admission"), outage_alerts.AlertDelivered)
                self.assertEqual(client.down_alert(), 0)
                self.assertEqual(sender.call_count, 1)
                with fixture.loop_turn():
                    fixture.server.run_once()
                self.assertEqual(client.down_alert(), 0)
                self.assertEqual(sender.call_count, 2)
            command, message = commands[0]
            self.assertEqual(command[command.index("--to") + 1], "builds")
            self.assertEqual(command[command.index("--from") + 1], "build-admission")
            self.assertEqual(message,
                "The build queue service has not answered for 10 seconds. Builds wait.\n"
                + "The service restarts by itself. Read journalctl -u build-admission.service.\n"
                + "If the user must act, push the phone with this command: "
                + "systemctl reset-failed build-admission.socket build-admission.service && systemctl start build-admission.socket build-admission.service")
            self.assertEqual(commands[1], commands[0])

    def test_down_alert_retains_sent_or_waiting_messages_and_retries_only_empty_deliveries(self) -> None:
        cases: tuple[tuple[send_result.Outcome, send_result.Outcome, bool], ...] = (
            ("queued", "sent", True), ("held", "failed", True), ("pending", "failed", True),
            ("queued", "failed", True), ("failed", "sent", True), ("failed", "queued", True),
            ("failed", "held", True), ("failed", "pending", True), ("failed", "skipped", True),
            ("skipped", "failed", True), ("failed", "failed", False),
            ("refused", "refused", False), ("retry", "retry", False))
        for session, phone, retained in cases:
            with self.subTest(session=session, phone=phone), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=2, reconnect_seconds=1)
                replies = [subprocess.CompletedProcess([], send_result.EXIT[outcome],
                    f"{outcome.upper()}: stub delivery\n", "") for outcome in (session, phone)]
                with mock.patch.object(client, "CLOCK", fixture.clock), \
                        mock.patch.object(client, "view", side_effect=wire.AdmissionUnavailable("no socket")), \
                        mock.patch.object(subprocess, "run", side_effect=replies * 2) as sender:
                    self.assertEqual(client.down_alert(), 0 if retained else 1)
                    self.assertEqual(sender.call_count, 2)
                    command = cast(list[str], sender.call_args_list[1].args[0])
                    self.assertEqual(command[command.index("--to") + 1], "user")
                    self.assertEqual(command[command.index("--text-id") + 1], "alert.blocked")
                    self.assertEqual(command[command.index("--need") + 1], "blocked")
                    values = [command[index + 1] for index, part in enumerate(command) if part == "--value"]
                    self.assertEqual(values, [
                        "action=On natedev, run: systemctl reset-failed build-admission.socket build-admission.service && systemctl start build-admission.socket build-admission.service",
                        "body=The builds session did not take the message. Builds wait until the service answers.",
                        "sender=build-admission", "title=Build queue service is down"])
                    scripts = Path(client.__file__).resolve().parents[1]
                    self.assertEqual(shlex.split(command[command.index("--clears-when") + 1]),
                        [str(scripts / "lib/py"), str(Path(client.__file__).resolve()), "view"])
                    self.assertEqual(client.down_alert(), 0 if retained else 1)
                    self.assertEqual(sender.call_count, 2 if retained else 4)

    def test_outage_clear_command_reports_failure_while_the_private_service_is_down(self) -> None:
        with AdmissionFixture() as fixture, redirect_stderr(io.StringIO()) as errors:
            fixture.server.close()
            self.assertNotEqual(client.main(["view"]), 0)
            self.assertIn("admission service unavailable:", errors.getvalue())

    def test_retry_and_outage_notice_intervals_read_config_during_the_call(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(reconnect_seconds=3, outage_notice_seconds=5)
            request = fixture.request()
            transport = self.use_transport(fixture,
                *(Exchange(connect_error=FileNotFoundError()) for _ in range(3)),
                Exchange(reply_bytes(GRANTED)))
            def reconfigure(_seconds: float) -> None:
                if fixture.clock.monotonic() == 3:
                    fixture.configure(reconnect_seconds=4, outage_notice_seconds=6)
            fixture.clock.on_sleep = reconfigure
            with redirect_stderr(io.StringIO()) as output, \
                    mock.patch.object(records, "read_memory_config", wraps=records.read_memory_config) as reader:
                self.assertEqual(client.admit(request, lambda _wait: None, lambda: request), GRANTED)
            reader.assert_called_once_with(report=False)
            self.assertEqual(transport.attempts, [0, 3, 6, 9])
            self.assertEqual(output.getvalue().count("admission service unavailable:"), 2)

    def test_unreachable_limit_stops_missing_and_refused_connections(self) -> None:
        for error in (FileNotFoundError(), ConnectionRefusedError()):
            with self.subTest(error=type(error).__name__), AdmissionFixture() as fixture:
                fixture.configure(reconnect_seconds=3, unreachable_limit_seconds=5)
                request = fixture.request()
                transport = self.use_transport(fixture, *(Exchange(connect_error=error) for _ in range(3)))
                with redirect_stderr(io.StringIO()) as output:
                    result = client.admit(request, lambda _wait: None, lambda: request)
                self.assertIsInstance(result, wire.Refused)
                self.assertEqual(fixture.clock.monotonic(), 5)
                self.assertEqual(transport.attempts, [0, 3])
                self.assertEqual(output.getvalue().count("build queue not reachable; trying for 5 s"), 1)
                self.assertEqual(output.getvalue().count(
                    "build queue unreachable for 5 s; step stopped; cargo token given back"), 1)
                self.doCleanups()

    def test_repeated_accepted_connections_without_replies_stop_at_the_unreachable_limit(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(unreachable_limit_seconds=0.3)
            request = fixture.request()
            transport = self.use_transport(fixture,
                *(Exchange(TimedChunk(0.125, b"")) for _ in range(8)))
            with redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, wire.Refused(
                "build queue unreachable for 0.3 s; step stopped; cargo token given back"))
            self.assertGreaterEqual(fixture.clock.monotonic(), 0.3)
            self.assertGreater(len(transport.requests), 1)
            self.assertEqual(transport.requests, [request] * len(transport.requests))
            self.assertEqual(output.getvalue().count(
                "build queue unreachable for 0.3 s; step stopped; cargo token given back"), 1)

    def test_valid_wait_after_restart_keeps_the_place_beyond_the_first_unreachable_deadline(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(unreachable_limit_seconds=0.3)
            request = fixture.request()
            waits: list[wire.Wait] = []
            transport = self.use_transport(fixture,
                Exchange(ConnectionResetError(errno.ECONNRESET, "reset")),
                Exchange(TimedChunk(0.15, reply_bytes(WAIT)), TimedChunk(0.25, b"")),
                Exchange(reply_bytes(WAIT) + reply_bytes(GRANTED)))
            with redirect_stderr(io.StringIO()) as output:
                started = fixture.clock.monotonic()
                result = client.admit(request, waits.append, lambda: request)
                elapsed = fixture.clock.monotonic() - started
            self.assertEqual(result, GRANTED)
            self.assertGreater(elapsed, 0.35)
            self.assertEqual(transport.requests, [request] * 3)
            self.assertEqual(waits, [WAIT])
            self.assertNotIn("step stopped", output.getvalue())

    def test_reset_then_accepted_silent_connection_stops_at_the_unreachable_limit(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(unreachable_limit_seconds=0.3, silence_seconds=5)
            request = fixture.request()
            transport = self.use_transport(fixture,
                Exchange(ConnectionResetError(errno.ECONNRESET, "reset")), Exchange(TimeoutError()))
            with redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, wire.Refused(
                "build queue unreachable for 0.3 s; step stopped; cargo token given back"))
            self.assertGreaterEqual(fixture.clock.monotonic(), 0.3)
            self.assertEqual(transport.requests, [request, request])
            self.assertEqual(output.getvalue().count(
                "build queue unreachable for 0.3 s; step stopped; cargo token given back"), 1)

    def test_a_valid_reply_after_the_unreachable_limit_keeps_the_wait(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(unreachable_limit_seconds=5)
            request = fixture.request()
            waits: list[wire.Wait] = []
            replacement = Exchange()
            transport = self.use_transport(fixture,
                Exchange(ConnectionResetError(errno.ECONNRESET, "reset")), replacement)

            def resume_after_deadline() -> None:
                fixture.clock.advance(1)
                replacement.events.extend((reply_bytes(WAIT), TimedChunk(0.01, reply_bytes(GRANTED))))

            transport.on_selector_timeout = resume_after_deadline
            with redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, waits.append, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertEqual(waits, [WAIT])
            self.assertEqual(transport.requests, [request, request])
            self.assertNotIn("step stopped", output.getvalue())

    def test_an_owner_that_left_past_the_unreachable_limit_gets_left_queue(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(running_pass_seconds=3, unreachable_limit_seconds=5)
            request = fixture.request()
            transport = self.use_transport(fixture,
                Exchange(ConnectionResetError(errno.ECONNRESET, "reset")),
                Exchange(TimedChunk(6, b"incomplete reply")))
            with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "test-leave"}), \
                    mock.patch.object(client, "_leave_queue", return_value=True) as leave, \
                    redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, client.LeftQueue())
            leave.assert_called_once_with()
            self.assertEqual(transport.requests, [request, request])
            self.assertNotIn("step stopped", output.getvalue())

    def test_a_valid_reply_resets_the_unreachable_limit(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(reconnect_seconds=3, unreachable_limit_seconds=5)
            request = fixture.request()
            transport = self.use_transport(fixture, Exchange(connect_error=FileNotFoundError()),
                Exchange(reply_bytes(WAIT), ConnectionResetError(errno.ECONNRESET, "reset")), Exchange(connect_error=FileNotFoundError()),
                Exchange(reply_bytes(GRANTED)))
            with redirect_stderr(io.StringIO()) as output:
                self.assertEqual(client.admit(request, lambda _wait: None, lambda: request), GRANTED)
            self.assertEqual(transport.attempts, [0, 3, 3, 6])
            self.assertEqual(output.getvalue().count("build queue not reachable; trying for 5 s"), 2)
            self.assertNotIn("step stopped", output.getvalue())

    def test_silence_notice_uses_the_configured_interval(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(silence_seconds=7)
            request = fixture.request()
            grant, replay = self.saved_grant_replay(fixture, request)
            transport = self.use_transport(fixture, Exchange(TimeoutError()), replay)
            with redirect_stderr(io.StringIO()) as output:
                self.assertEqual(client.admit(request, lambda _wait: None, lambda: request), grant)
            self.assertEqual(fixture.clock.monotonic(), 7)
            self.assertEqual(transport.selector_timeouts, [7])
            self.assertEqual(transport.timeouts, [])
            self.assertEqual(transport.attempts, [0, 7])
            self.assertEqual(transport.requests, [request, request])
            self.assertEqual(output.getvalue().count("admission service not answering since"), 1)

    def test_edited_transport_timing_applies_to_the_next_call(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            fixture.configure(reconnect_seconds=3, outage_notice_seconds=5)
            transport = self.use_transport(fixture,
                Exchange(connect_error=FileNotFoundError()), Exchange(reply_bytes(GRANTED)),
                Exchange(connect_error=FileNotFoundError()), Exchange(reply_bytes(GRANTED)))
            with redirect_stderr(io.StringIO()), \
                    mock.patch.object(records, "read_memory_config", wraps=records.read_memory_config) as reader:
                self.assertEqual(client.admit(request, lambda _wait: None, lambda: request), GRANTED)
                fixture.configure(reconnect_seconds=7, outage_notice_seconds=11)
                self.assertEqual(client.admit(request, lambda _wait: None, lambda: request), GRANTED)
            self.assertEqual(reader.call_args_list, [mock.call(report=False), mock.call(report=False)])
            self.assertEqual(transport.attempts, [0, 3, 3, 10])

    def test_recovered_service_setting_warns_once_at_the_client_boundary(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, estimate_refresh_seconds="321")
            commit_config_fixture(config)
            write_memory_config(config, estimate_refresh_seconds="invalid")
            request = fixture.request()
            with fixture.serving(), mock.patch.object(records, "_warned_configuration_diagnostics", set[records.ConfigurationDiagnostic]()), \
                    mock.patch.object(client, "CLOCK", fixture.clock), redirect_stderr(io.StringIO()) as errors:
                first = client.admit(request, lambda _wait: None, lambda: request)
                second = client.admit(request, lambda _wait: None, lambda: request)
            self.assertIsInstance(first, wire.Admitted)
            self.assertEqual(second, first)
            policy = fixture.server.last_policy
            assert isinstance(policy, records.AdmissionPolicy)
            self.assertEqual(policy.timing.estimate_refresh_seconds, 321)
            self.assertIn(str(config), errors.getvalue())
            self.assertIn("estimate_refresh_seconds='invalid'", errors.getvalue())
            self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_wait_callback_then_admission_accepts_new_source_names(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            transport = self.use_transport(fixture, Exchange(reply_bytes(WAIT), TimedChunk(0.01, reply_bytes(GRANTED))))
            waits: list[wire.Wait] = []
            result = client.admit(request, waits.append, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertEqual(waits, [WAIT])
            self.assertEqual(transport.requests, [request])

    def test_explicit_refusal_is_terminal_without_retry_or_refresh(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            refusal = wire.Refused("client version 2, service supports 1..1")
            transport = self.use_transport(fixture, Exchange(reply_bytes(refusal)))
            refresh = mock.Mock(side_effect=AssertionError("must not refresh refusal"))
            self.assertEqual(client.admit(request, lambda _wait: None, refresh), refusal)
            self.assertEqual(len(transport.attempts), 1)
            refresh.assert_not_called()
            self.assertFalse(fixture.clock.sleeps)

    def test_no_socket_and_refused_connect_retry_every_two_seconds_from_first_call(self) -> None:
        for error in (FileNotFoundError("missing"), ConnectionRefusedError("refused")):
            with self.subTest(error=type(error).__name__), AdmissionFixture() as fixture:
                fixture.configure(unreachable_limit_seconds=120)
                request = fixture.request()
                failures = [Exchange(connect_error=error) for _ in range(31)]
                transport = self.use_transport(fixture, *failures, Exchange(reply_bytes(wire.Refused("test stop"))))
                with redirect_stderr(io.StringIO()) as output:
                    result = client.admit(request, lambda _wait: None, lambda: request)
                self.assertEqual(result, wire.Refused("test stop"))
                self.assertEqual(transport.attempts, [2.0 * index for index in range(32)])
                self.assertEqual(output.getvalue().count("admission service unavailable:"), 2)
                self.assertEqual(transport.requests, [request])
                self.doCleanups()

    def test_a_silent_connection_reconnects_and_replays_the_saved_grant(self) -> None:
        for kind in ("admit", "launch"):
            with self.subTest(kind=kind), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=2, reconnect_seconds=0.05, unreachable_limit_seconds=10)
                fixture.hold_starts()
                request = (fixture.request(age=20) if kind == "admit"
                           else fixture.launch(since=fixture.clock.now() - 20))
                younger = fixture.request()
                self.assertIsInstance(fixture.connect(younger).replies()[0], wire.Wait)
                fixture.clock.advance(1)
                server = fixture.server
                send = server._send  # pyright: ignore[reportPrivateUsage]
                connections: list[service.Connection] = []
                grants: list[tuple[service.Connection, wire.Reply]] = []
                orders: list[list[int]] = []

                def drop_first_wait_and_grant(connection: service.Connection, reply: wire.Reply) -> None:
                    if connection.request == request:
                        if all(connection is not previous for previous in connections):
                            connections.append(connection)
                        if connection is connections[0]:
                            return
                        if isinstance(reply, (wire.Admitted, wire.LaunchAdmitted)):
                            if all(connection is not previous for previous, _reply in grants):
                                grants.append((connection, reply))
                            if connection is grants[0][0]:
                                return
                    send(connection, reply)

                def allow_grant(_reply: wire.Wait) -> None:
                    orders.append(fixture.queue_pids())
                    write_pressure_reading(fixture.root / "pressure")
                    _ = fixture.run_pass()

                def check_replay_deadline() -> None:
                    self.assertLess(fixture.clock.monotonic(), 10, "the client did not receive the replay")

                with (mock.patch.object(client, "CLOCK", fixture.clock),
                      mock.patch.object(client, "DefaultSelector",
                          side_effect=partial(PrivateSocketSelector, fixture, before_read=check_replay_deadline)),
                      mock.patch.object(server, "_send", side_effect=drop_first_wait_and_grant),
                      mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "", "BUILDLOG_MAC_IDLE_HOOK": ""}),
                      redirect_stderr(io.StringIO())):
                    if isinstance(request, wire.AdmitRequest):
                        admit_request = request
                        result = client.admit(admit_request, allow_grant, lambda: admit_request)
                    else:
                        launch_request = request
                        result = client.launch(launch_request, allow_grant, lambda: launch_request)
                self.assertIsInstance(result, wire.Admitted if kind == "admit" else wire.LaunchAdmitted)
                self.assertEqual(orders, [[request.owner.pid, younger.owner.pid]])
                self.assertEqual(len(connections), 3)
                self.assertEqual([connection.request for connection in connections], [request] * 3)
                self.assertEqual([connection for connection, _reply in grants], connections[1:])
                self.assertEqual([reply for _connection, reply in grants], [result, result])
                saved = service.read_state(fixture.state_dir)
                self.assertEqual(saved.ledger[request.episode].reply, result)
                self.assertEqual(request.episode in saved.reservations, kind == "admit")

    def test_a_silent_reconnect_keeps_the_head_when_pressure_ends_at_resend(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(silence_seconds=2, unreachable_limit_seconds=10)
            fixture.free(50 * GIB)
            fixture.hold_starts()
            older = fixture.request(age=20, need=40 * GIB)
            younger = fixture.request(need=40 * GIB)
            self.assertIsInstance(fixture.connect(younger).replies()[0], wire.Wait)
            fixture.clock.advance(1)
            server = fixture.server
            send = server._send  # pyright: ignore[reportPrivateUsage]
            connect = client._acquisition_socket  # pyright: ignore[reportPrivateUsage]
            attempts: list[float] = []

            def keep_wait_silent(connection: service.Connection, reply: wire.Reply) -> None:
                if not (connection.request == older and isinstance(reply, wire.Wait)):
                    send(connection, reply)

            def connect_at_pressure_change() -> socket.socket:
                attempts.append(fixture.clock.monotonic())
                if len(attempts) == 2:
                    write_pressure_reading(fixture.root / "pressure")
                    server.run_once(0)
                    _ = fixture.run_pass()
                    self.assertEqual(set(server.reservations), {older.episode},
                                     "the older waiter must receive the first grant")
                return connect()

            def check_grant_deadline() -> None:
                self.assertLess(fixture.clock.monotonic(), 10, "the client did not receive the grant")

            with (mock.patch.object(client, "CLOCK", fixture.clock),
                  mock.patch.object(client, "_acquisition_socket", side_effect=connect_at_pressure_change),
                  mock.patch.object(client, "DefaultSelector",
                      side_effect=partial(PrivateSocketSelector, fixture, before_read=check_grant_deadline)),
                  mock.patch.object(server, "_send", side_effect=keep_wait_silent),
                  mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "", "BUILDLOG_MAC_IDLE_HOOK": ""}),
                  redirect_stderr(io.StringIO())):
                result = client.admit(older, lambda _reply: None, lambda: older)
            self.assertIsInstance(result, wire.Admitted)
            self.assertEqual(attempts, [1, 3])
            self.assertEqual(service.read_state(fixture.state_dir).ledger[older.episode].reply, result)

    def test_a_buffered_wait_keeps_the_live_resend_at_the_head(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(silence_seconds=2, unreachable_limit_seconds=10)
            fixture.free(50 * GIB)
            fixture.hold_starts()
            older = fixture.request(age=20, need=40 * GIB)
            younger = fixture.request(need=40 * GIB)
            self.assertIsInstance(fixture.connect(younger).replies()[0], wire.Wait)
            server = fixture.server
            send = server._send  # pyright: ignore[reportPrivateUsage]
            connect = client._acquisition_socket  # pyright: ignore[reportPrivateUsage]
            read_line = client._line  # pyright: ignore[reportPrivateUsage]
            connections: list[service.Connection] = []
            original_waits: list[wire.Wait] = []
            grants: list[service.Connection] = []

            def delay_old_wait(connection: service.Connection, reply: wire.Reply) -> None:
                if connection.request == older:
                    if all(connection is not previous for previous in connections):
                        connections.append(connection)
                    if isinstance(reply, wire.Wait):
                        if connection is connections[0]:
                            original_waits.append(reply)
                        return
                    if isinstance(reply, wire.Admitted):
                        grants.append(connection)
                send(connection, reply)

            def connect_with_buffered_wait() -> socket.socket:
                if original_waits:
                    original = connections[0]
                    original.socket.sendall(server.reply_bytes(original_waits[-1], version=original.version))
                return connect()

            def read_on_private_socket(connection: socket.socket, pending: bytearray, deadline: float) -> bytes:
                await_private_socket(fixture, connection, deadline - fixture.clock.monotonic())
                return read_line(connection, pending, deadline)

            def allow_grant(_reply: wire.Wait) -> None:
                self.assertEqual(len(connections), 2)
                self.assertIs(server.waiters[older.episode].connection, connections[1])
                write_pressure_reading(fixture.root / "pressure")
                server.run_once(0)
                _ = fixture.run_pass()
                self.assertEqual(set(server.reservations), {older.episode},
                                 "the older waiter must receive the first grant")

            with (mock.patch.object(client, "CLOCK", fixture.clock),
                  mock.patch.object(client, "_acquisition_socket", side_effect=connect_with_buffered_wait),
                  mock.patch.object(client, "_line", side_effect=read_on_private_socket),
                  mock.patch.object(client, "DefaultSelector", side_effect=partial(PrivateSocketSelector, fixture)),
                  mock.patch.object(server, "_send", side_effect=delay_old_wait),
                  redirect_stderr(io.StringIO())):
                result = client.admit(older, allow_grant, lambda: older)
            self.assertIsInstance(result, wire.Admitted)
            self.assertEqual(grants, connections[1:])
            self.assertEqual(service.read_state(fixture.state_dir).ledger[older.episode].reply, result)

    def test_a_ready_grant_wins_over_a_wait_on_the_other_socket(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(silence_seconds=2, unreachable_limit_seconds=10)
            request = fixture.request()
            grant, replay = self.saved_grant_replay(fixture, request)
            transport = self.use_transport(fixture, Exchange(TimeoutError(), reply_bytes(WAIT), b""),
                replay, Exchange(reply_bytes(wire.Refused("the client lost the ready grant"))))
            with redirect_stderr(io.StringIO()):
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, grant)
            self.assertEqual(transport.requests, [request, request])

    def test_a_silent_resend_reads_a_grant_on_a_high_descriptor(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(silence_seconds=2, unreachable_limit_seconds=10)
            request = fixture.request()
            grant, _replay = self.saved_grant_replay(fixture, request)
            original, original_peer = socket.socketpair()
            resend, resend_peer = socket.socketpair()
            with original, original_peer, resend, resend_peer:
                # Reserve an unused descriptor before dup2 replaces it.
                soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
                if 0 <= hard <= 1024:
                    self.skipTest("the process cannot open descriptor 1024")
                resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 1025), hard))
                try:
                    descriptor = fcntl.fcntl(resend, fcntl.F_DUPFD_CLOEXEC, 1024)
                    _ = os.dup2(resend.fileno(), descriptor, inheritable=False)
                finally:
                    resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
                with socket.socket(fileno=descriptor) as high_socket:
                    resend_peer.sendall(reply_bytes(grant))

                    def time_out(_connection: socket.socket, _pending: bytearray, deadline: float) -> bytes:
                        fixture.clock.advance(deadline - fixture.clock.monotonic())
                        raise TimeoutError("original socket is silent")

                    with (mock.patch.object(client, "CLOCK", fixture.clock),
                          mock.patch.object(client, "_acquisition_socket", side_effect=[original, high_socket]),
                          mock.patch.object(client, "_line", side_effect=time_out),
                          mock.patch.object(client, "DefaultSelector", side_effect=partial(PrivateSocketSelector, fixture)),
                          redirect_stderr(io.StringIO())):
                        result = client.admit(request, lambda _wait: None, lambda: request)
                    self.assertEqual(result, grant)

    def test_a_grant_on_the_original_socket_ends_a_silent_resend(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(silence_seconds=2, unreachable_limit_seconds=9)
            request = fixture.request()
            grant, _replay = self.saved_grant_replay(fixture, request)
            transport = self.use_transport(fixture, Exchange(TimeoutError(), TimedChunk(0.01, reply_bytes(grant))),
                                           *(Exchange(TimeoutError()) for _ in range(6)))
            with redirect_stderr(io.StringIO()):
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, grant)
            self.assertEqual(transport.requests, [request, request])
            self.assertTrue(transport.sockets[1].closed)
            self.assertTrue(transport.sockets[0].closed)

    def test_history_timeout_and_grant_use_the_owned_admission_sockets(self) -> None:
        for delayed in (True, False):
            with self.subTest(delayed=delayed), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=2, reconnect_seconds=0.5, unreachable_limit_seconds=8)
                fixture.free(50 * GIB)
                fixture.hold_starts()
                older = fixture.request(age=20, need=40 * GIB)
                younger = fixture.request(need=40 * GIB)
                self.assertIsInstance(fixture.connect(younger).replies()[0], wire.Wait)
                server = fixture.server
                send = server._send  # pyright: ignore[reportPrivateUsage]
                connect = client._acquisition_socket  # pyright: ignore[reportPrivateUsage]
                read_line = client._line  # pyright: ignore[reportPrivateUsage]
                handle = server.handle_request
                sockets: list[socket.socket] = []
                histories: list[tuple[service.Connection, float]] = []
                deferred_waits: list[tuple[service.Connection, wire.Wait]] = []
                grants: list[wire.Admitted] = []

                def grant_head() -> None:
                    assert histories
                    self.assertIs(server.waiters[older.episode].connection, histories[0][0])
                    self.assertEqual(fixture.queue_pids(), [older.owner.pid, younger.owner.pid])
                    write_pressure_reading(fixture.root / "pressure")
                    _ = fixture.run_pass()

                def delayed_history(request: wire.Request,
                                    connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                                    ) -> list[wire.Reply]:
                    if not isinstance(request, wire.BuildHistoryRequest):
                        return handle(request, connection)
                    assert isinstance(connection, service.Connection)
                    self.assertTrue(all(peer.fileno() >= 0 for peer in sockets))
                    histories.append((connection, fixture.clock.monotonic()))
                    if not delayed:
                        grant_head()
                    return []

                def expire_head(connection: service.Connection, reply: wire.Reply) -> None:
                    if connection.request == older:
                        if isinstance(reply, wire.Admitted):
                            grants.append(reply)
                        if isinstance(reply, wire.Wait) and not histories:
                            if delayed and len(sockets) == 1:
                                deferred_waits.append((connection, reply))
                                return
                            reply = EXPIRED
                    send(connection, reply)

                def dial() -> socket.socket:
                    self.assertLess(len(sockets), 2 if delayed else 1, "history opened another socket")
                    if deferred_waits:
                        original, wait = deferred_waits[-1]
                        original.socket.sendall(server.reply_bytes(wait, version=original.version))
                    peer = connect()
                    sockets.append(peer)
                    return peer

                def delay_original_eof(key: SelectorKey) -> bool:
                    if not delayed or len(sockets) != 2 or histories or key.fileobj is not sockets[0]:
                        return False
                    try:
                        return sockets[0].recv(1, socket.MSG_PEEK) == b""
                    except BlockingIOError:
                        return False

                def on_timeout() -> None:
                    if delayed and histories and fixture.clock.monotonic() - histories[0][1] >= 0.5 and not grants:
                        grant_head()

                def read_on_private_socket(connection: socket.socket, pending: bytearray, deadline: float) -> bytes:
                    with PrivateSocketSelector(fixture, on_timeout) as selector:
                        _ = selector.register(connection, EVENT_READ)
                        _ = selector.select(deadline - fixture.clock.monotonic())
                    return read_line(connection, pending, deadline)

                def refresh() -> wire.AdmitRequest:
                    _ = client.build_history_generation()
                    return older

                with (mock.patch.object(client, "CLOCK", fixture.clock),
                      mock.patch.object(client, "_acquisition_socket", side_effect=dial),
                      mock.patch.object(client, "_line", side_effect=read_on_private_socket),
                      mock.patch.object(client, "DefaultSelector",
                          side_effect=partial(PrivateSocketSelector, fixture, on_timeout, hide_read=delay_original_eof)),
                      mock.patch.object(server, "handle_request", side_effect=delayed_history),
                      mock.patch.object(server, "_send", side_effect=expire_head),
                      redirect_stderr(io.StringIO())):
                    result = client.admit(older, lambda _wait: None, refresh)
                self.assertIsInstance(result, wire.Admitted)
                self.assertEqual(len(histories), 1)
                self.assertEqual(grants, [result])
                self.assertEqual(len(sockets), 2 if delayed else 1)
                self.assertEqual(fixture.queue_pids(), [younger.owner.pid])

    def test_checker_and_estimator_errors_preserve_the_head_connection(self) -> None:
        for estimator_error in (False, True):
            with self.subTest(estimator_error=estimator_error), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=4, reconnect_seconds=1, unreachable_limit_seconds=9)
                fixture.free(50 * GIB)
                fixture.hold_starts()
                older = fixture.request(age=20, need=40 * GIB)
                younger = fixture.request(need=40 * GIB)
                self.assertIsInstance(fixture.connect(younger).replies()[0], wire.Wait)
                server = fixture.server
                send = server._send  # pyright: ignore[reportPrivateUsage]
                read_line = client._line  # pyright: ignore[reportPrivateUsage]
                initials: list[service.Connection] = []
                attempts: list[float] = []
                checked: list[float] = []

                def send_error(connection: service.Connection, reply: wire.Reply) -> None:
                    if connection.request == older and isinstance(reply, wire.Wait) and not initials:
                        initials.append(connection)
                        if estimator_error:
                            reply = EXPIRED
                        else:
                            send(connection, reply)
                            reply = wait_reply(records.ReadingUnavailable("fixture reading failed"))
                    send(connection, reply)

                def recover() -> None:
                    if not initials or checked or fixture.clock.monotonic() < 1:
                        return
                    server.run_once(0)
                    self.assertIn(older.episode, server.waiters, "the live waiter disappeared during retry")
                    self.assertIs(server.waiters[older.episode].connection, initials[0])
                    self.assertEqual(fixture.queue_pids(), [older.owner.pid, younger.owner.pid])
                    checked.append(fixture.clock.monotonic())
                    write_pressure_reading(fixture.root / "pressure")
                    _ = fixture.run_pass()

                def refresh() -> wire.AdmitRequest:
                    attempts.append(fixture.clock.monotonic())
                    if len(attempts) == 1:
                        raise OSError(errno.ESTALE, "fixture estimator failed")
                    return older

                def read_on_private_socket(connection: socket.socket, pending: bytearray, deadline: float) -> bytes:
                    await_private_socket(fixture, connection, deadline - fixture.clock.monotonic())
                    return read_line(connection, pending, deadline)

                fixture.clock.on_sleep = lambda _seconds: recover()
                with (mock.patch.object(client, "CLOCK", fixture.clock),
                      mock.patch.object(client, "_line", side_effect=read_on_private_socket),
                      mock.patch.object(client, "DefaultSelector", side_effect=partial(PrivateSocketSelector, fixture, recover)),
                      mock.patch.object(server, "_send", side_effect=send_error), redirect_stderr(io.StringIO())):
                    result = client.admit(older, lambda _wait: None, refresh)
                self.assertIsInstance(result, wire.Admitted)
                self.assertEqual(len(checked), 1)
                self.assertEqual(set(server.reservations), {older.episode})

    def test_unavailable_reading_retries_only_after_a_valid_wait(self) -> None:
        reason = "fixture reading failed"
        unavailable = wait_reply(records.ReadingUnavailable(reason))
        for already_waiting in (False, True):
            with self.subTest(already_waiting=already_waiting), AdmissionFixture() as fixture:
                request = fixture.service_request()
                events = (reply_bytes(WAIT), TimedChunk(0.5, reply_bytes(unavailable)),
                          TimedChunk(2, reply_bytes(GRANTED))) if already_waiting else (reply_bytes(unavailable),)
                transport = self.use_transport(fixture, Exchange(*events))
                waits: list[wire.Wait] = []
                try:
                    with (mock.patch.dict(os.environ, {"BUILDLOG_MEM_CHECK_RETRY_S": "3", "LINT_QUEUE_LEAVE_HOOK": "",
                                                       "BUILDLOG_MAC_IDLE_HOOK": ""}), redirect_stderr(io.StringIO()) as output):
                        result = client.service_admit(request, waits.append, wire.AcquisitionTiming(5, 1, 10))
                finally:
                    self.doCleanups()
                self.assertEqual(transport.requests, [request])
                self.assertTrue(transport.sockets[0].closed)
                if already_waiting:
                    self.assertEqual(result, GRANTED)
                    self.assertEqual(waits, [WAIT])
                    self.assertEqual(output.getvalue(), f"memory admission check failed ({reason}); trying again, "
                                     + "the step keeps its place\n")
                else:
                    self.assertEqual(result, wire.Refused(reason))
                    self.assertEqual(fixture.clock.monotonic(), 0)
                    self.assertEqual(waits, [])
                    self.assertEqual(output.getvalue(), f"memory admission check failed ({reason}); step refused\n")

    def test_a_failed_replacement_retries_inside_the_original_deadline(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(silence_seconds=2, reconnect_seconds=1, unreachable_limit_seconds=5)
            fixture.free(50 * GIB)
            fixture.hold_starts()
            older = fixture.request(age=20, need=40 * GIB)
            younger = fixture.request(need=40 * GIB)
            self.assertIsInstance(fixture.connect(younger).replies()[0], wire.Wait)
            server = fixture.server
            send = server._send  # pyright: ignore[reportPrivateUsage]
            connect = client._acquisition_socket  # pyright: ignore[reportPrivateUsage]
            read_line = client._line  # pyright: ignore[reportPrivateUsage]
            attempts: list[float] = []

            def mute_original(connection: service.Connection, reply: wire.Reply) -> None:
                if connection.request != older or not isinstance(reply, wire.Wait):
                    send(connection, reply)

            def dial() -> socket.socket:
                attempts.append(fixture.clock.monotonic())
                if len(attempts) == 2:
                    raise ConnectionRefusedError("fixture replacement failed")
                if len(attempts) == 3:
                    self.assertLess(attempts[-1], attempts[1] + 5)
                    self.assertEqual(fixture.queue_pids(), [older.owner.pid, younger.owner.pid])
                    write_pressure_reading(fixture.root / "pressure")
                return connect()

            def read_on_private_socket(connection: socket.socket, pending: bytearray, deadline: float) -> bytes:
                await_private_socket(fixture, connection, deadline - fixture.clock.monotonic())
                return read_line(connection, pending, deadline)

            with (mock.patch.object(client, "CLOCK", fixture.clock),
                  mock.patch.object(client, "_acquisition_socket", side_effect=dial),
                  mock.patch.object(client, "_line", side_effect=read_on_private_socket),
                  mock.patch.object(client, "DefaultSelector", side_effect=partial(PrivateSocketSelector, fixture)),
                  mock.patch.object(server, "_send", side_effect=mute_original), redirect_stderr(io.StringIO())):
                result = client.admit(older, lambda _wait: None, lambda: older)
            self.assertIsInstance(result, wire.Admitted)
            self.assertEqual(len(attempts), 3)
            self.assertEqual(set(server.reservations), {older.episode})

    def test_the_first_admission_read_uses_the_selector_on_a_high_descriptor(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(50 * GIB)
            fixture.hold_starts()
            older = fixture.request(age=20, need=40 * GIB)
            younger = fixture.request(need=40 * GIB)
            self.assertIsInstance(fixture.connect(younger).replies()[0], wire.Wait)
            connect = client._acquisition_socket  # pyright: ignore[reportPrivateUsage]

            def dial() -> socket.socket:
                with connect() as peer:
                    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
                    if 0 <= hard <= 1024:
                        self.skipTest("the process cannot open descriptor 1024")
                    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 1025), hard))
                    try:
                        descriptor = fcntl.fcntl(peer, fcntl.F_DUPFD_CLOEXEC, 1024)
                        _ = os.dup2(peer.fileno(), descriptor, inheritable=False)
                    finally:
                        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
                    return socket.socket(fileno=descriptor)

            def allow_head(_reply: wire.Wait) -> None:
                self.assertEqual(fixture.queue_pids(), [older.owner.pid, younger.owner.pid])
                write_pressure_reading(fixture.root / "pressure")
                _ = fixture.run_pass()

            with (mock.patch.object(client, "CLOCK", fixture.clock),
                  mock.patch.object(client, "_acquisition_socket", side_effect=dial),
                  mock.patch.object(client, "_line", side_effect=AssertionError("the first admission read bypassed the selector")),
                  mock.patch.object(client, "DefaultSelector", side_effect=partial(PrivateSocketSelector, fixture))):
                result = client.admit(older, allow_head, lambda: older)
            self.assertIsInstance(result, wire.Admitted)
            self.assertEqual(fixture.clock.monotonic(), 0)

    def test_a_silent_episode_sends_only_one_resend(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(silence_seconds=2, unreachable_limit_seconds=9)
            request = fixture.request()
            transport = self.use_transport(fixture, *(Exchange(TimeoutError()) for _ in range(7)))
            with redirect_stderr(io.StringIO()):
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, wire.Refused(
                "build queue unreachable for 9 s; step stopped; cargo token given back"))
            self.assertEqual(transport.attempts, [0, 2])
            self.assertEqual(transport.requests, [request, request])
            self.assertTrue(all(connection.closed for connection in transport.sockets))

    def test_closed_connection_reconnects_immediately_with_same_episode_and_since(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            gpu_wait = reply_bytes(wait_reply(records.GpuSlotWait(1, 1), 1, "head", "GPU test slot"))
            transport = self.use_transport(fixture, Exchange(reply_bytes(WAIT), b""),
                Exchange(gpu_wait, gpu_wait, b""), Exchange(reply_bytes(WAIT) + reply_bytes(GRANTED)))
            with redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertEqual(transport.requests, [request] * 3)
            self.assertEqual(transport.attempts, [0, 0, 0])
            self.assertEqual(output.getvalue().count("admission service restarted; waiting again"), 1)
            self.assertEqual([line for line in output.getvalue().splitlines() if line.startswith("queue reached again")],
                ["queue reached again: GPU test slot", "queue reached again: free memory"])

    def test_silent_service_reports_after_120_seconds_and_keeps_waiting(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            grant, replay = self.saved_grant_replay(fixture, request)
            transport = self.use_transport(fixture, Exchange(TimeoutError()), replay)
            with redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, grant)
            self.assertEqual(fixture.clock.monotonic(), 120)
            self.assertEqual(transport.attempts, [0, 120])
            self.assertEqual(transport.requests, [request, request])
            self.assertIn("admission service not answering since 12:00", output.getvalue())

    def saved_grant_replay(self, fixture: AdmissionFixture, request: wire.AdmitRequest | wire.LaunchRequest
                           ) -> tuple[wire.Admitted | wire.LaunchAdmitted, Exchange]:
        server = fixture.server
        grant, = server.handle_request(request)
        self.assertIsInstance(grant, wire.Admitted if isinstance(request, wire.AdmitRequest) else wire.LaunchAdmitted)
        assert isinstance(grant, (wire.Admitted, wire.LaunchAdmitted))

        def answer(resend: wire.Request) -> wire.Reply:
            self.assertEqual(resend, request)
            self.assertEqual(service.read_state(fixture.state_dir).ledger[request.episode].reply, grant)
            replay, = server.handle_request(resend)
            self.assertEqual(replay, grant)
            return replay

        return grant, Exchange(answer=answer)

    def test_queries_are_bounded_without_retry_and_cli_reports_reason(self) -> None:
        queries: tuple[tuple[str, Callable[[], object]], ...] = (
            ("view", client.view), ("explain", client.explain), ("mine", lambda: client.mine("session")))
        for name, query in queries:
            for event, reason in ((Exchange(connect_error=FileNotFoundError()), "no socket"),
                                  (Exchange(connect_error=ConnectionRefusedError()), "connect refused"),
                                  (Exchange(TimeoutError()), "no answer in 0.5 s")):
                with self.subTest(query=name, reason=reason), AdmissionFixture() as fixture:
                    transport = self.use_transport(fixture, event)
                    with self.assertRaises(wire.AdmissionUnavailable) as error:
                        _ = query()
                    self.assertEqual(error.exception.reason, reason)
                    self.assertLessEqual(fixture.clock.monotonic(), 0.5)
                    self.assertEqual(len(transport.attempts), 1)
                    self.assertFalse(fixture.clock.sleeps)
                    self.doCleanups()
            with self.subTest(cli=name), AdmissionFixture() as fixture:
                _ = self.use_transport(fixture, Exchange(connect_error=FileNotFoundError()))
                argv = ["admission_client", name] + (["session"] if name == "mine" else [])
                with mock.patch.object(sys, "argv", argv), redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(client.main(), 2)
                self.assertIn("admission service unavailable: no socket", output.getvalue())
                self.doCleanups()

    def test_placement_status_waits_for_slow_answers_and_refuses_dead_services(self) -> None:
        # The Mac gate reads this on each cargo call; a busy service must not refuse the step.
        from scripts.build_queue import admission_wire as status_wire

        unknown = status_wire.PlacementStatusReply(status_wire.UnknownPlacementStatus("episode-a", "attempt-a"))
        cases: tuple[tuple[str, str], ...] = (
            ("slow", ""), ("missing", "no socket"), ("refused", "connect refused"))
        for service_state, reason in cases:
            with self.subTest(service=service_state), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=1)
                response = (Exchange(TimeoutError(), reply_bytes(unknown)) if service_state == "slow" else
                            Exchange(connect_error=FileNotFoundError()) if service_state == "missing" else
                            Exchange(connect_error=ConnectionRefusedError()))
                transport = self.use_transport(fixture, response)
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                    result = client.main(["placement-status", "--episode", "episode-a", "--attempt", "attempt-a"])
                if service_state == "slow":
                    self.assertEqual(result, 1)
                    self.assertEqual(output.getvalue(), "unknown\t-\t-\t-\t-\t-\n")
                    self.assertIn("admission service not answering", errors.getvalue())
                    self.assertGreater(fixture.clock.monotonic(), 0.5)
                    self.assertEqual(transport.requests, [replace(status_wire.PlacementStatusRequest("episode-a", "attempt-a"),
                        code_version=client.LOADED_ADMISSION_CODE_VERSION)])
                else:
                    self.assertEqual(result, 2)
                    self.assertEqual(errors.getvalue(), f"admission service unavailable: {reason}\n")
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual(fixture.clock.monotonic(), 0)
                    self.assertFalse(transport.requests)
                self.assertEqual(len(transport.attempts), 1)
                self.assertFalse(fixture.clock.sleeps)
                self.doCleanups()

    def test_lint_cancellation_waits_for_slow_answers_and_refuses_dead_services(self) -> None:
        cases: tuple[tuple[str, str], ...] = (
            ("slow", ""), ("missing", "no socket"), ("refused", "connect refused"))
        for service_state, reason in cases:
            with self.subTest(service=service_state), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=1)
                start = records.process_start_time(os.getpid())
                assert start is not None
                submitted = fixture.request(step="lint", owner=wire.Owner(os.getpid(), start))
                completed = wire.Ok()
                response = (Exchange(TimeoutError(), reply_bytes(completed)) if service_state == "slow" else
                            Exchange(connect_error=FileNotFoundError()) if service_state == "missing" else
                            Exchange(connect_error=ConnectionRefusedError()))
                transport = self.use_transport(fixture, response)
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                    result = client.main(["cancel-placement", "--episode", submitted.episode,
                        "--pid", str(submitted.owner.pid), "--reason", "judge lint before local repair"])
                if service_state == "slow":
                    self.assertEqual(result, 0)
                    self.assertEqual(output.getvalue(), "")
                    self.assertIn("admission service not answering", errors.getvalue())
                    self.assertGreater(fixture.clock.monotonic(), 0.5)
                    request, = transport.requests
                    self.assertIsInstance(request, wire.CancelPlacement)
                    assert isinstance(request, wire.CancelPlacement)
                    self.assertEqual(request.episode, submitted.episode)
                    self.assertEqual(request.owner.pid, submitted.owner.pid)
                    self.assertEqual(request.owner.start_time, submitted.owner.start_time)
                    self.assertEqual(request.reason, "judge lint before local repair")
                else:
                    self.assertEqual(result, 2)
                    self.assertEqual(errors.getvalue(), f"admission service unavailable: {reason}\n")
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual(fixture.clock.monotonic(), 0)
                    self.assertFalse(transport.requests)
                self.assertEqual(len(transport.attempts), 1)
                self.assertFalse(fixture.clock.sleeps)
                self.doCleanups()

    def test_placement_control_waits_for_slow_answers_after_a_memory_handoff(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import (
            PlacementFixture, adopt_assignment, assignment_for, consume_permission, placement_request, publish_candidate,
        )

        for operation in ("claim", "permission", "execution", "cancel", "memory-handoff"):
            for service_state in ("slow", "missing", "refused"):
                with self.subTest(operation=operation, service=service_state), PlacementFixture() as fixture:
                    fixture.configure(silence_seconds=1)
                    submitted = placement_request(fixture)
                    _ = publish_candidate(fixture, submitted)
                    assigned = assignment_for(fixture, submitted)
                    claim = adopt_assignment(fixture, submitted, assigned)
                    query: Callable[[], object]
                    if operation == "claim":
                        failure = wire.ClaimFailure("failed-claim", assigned, "busy", claim)
                        request = failure
                        query = lambda: client.claim_failure(failure)
                    elif operation in ("permission", "memory-handoff"):
                        permission = consume_permission("launch-permission", assigned, claim)
                        request = permission
                        query = lambda: client.consume_permission(permission)
                    elif operation == "execution":
                        execution = wire.ExecutionReport("finished-step", assigned, claim,
                            execution_identity.ExecutionNotStarted(), placement.AttemptCompleted(0), placement.DispatchPathSettled())
                        request = execution
                        query = lambda: client.report_execution(execution)
                    else:
                        cancellation = wire.CancelPlacement("cancel-step", assigned.episode, assigned.owner, "lint repair")
                        request = cancellation
                        query = lambda: client.cancel_placement(cancellation)
                    expected, = fixture.server.handle_request(request)
                    response = (Exchange(TimeoutError(), reply_bytes(expected)) if service_state == "slow" else
                                Exchange(connect_error=FileNotFoundError()) if service_state == "missing" else
                                Exchange(connect_error=ConnectionRefusedError()))
                    exchanges: list[Exchange] = []
                    waiting = wire.PlacementWait(submitted.episode, submitted.since, 1, (), WAIT)
                    if operation == "memory-handoff":
                        exchanges.extend((Exchange(reply_bytes(waiting)), Exchange(reply_bytes(wire.Assignment(assigned)))))
                    transport = self.use_transport(fixture, *exchanges, response)
                    with redirect_stderr(io.StringIO()) as errors:
                        if operation == "memory-handoff":
                            self.assertEqual(client.request_placement(submitted), waiting)
                            output = client.WaitOutput()
                            output(WAIT)
                            fixture.clock.advance(705)
                            self.assertEqual(client.request_placement(submitted), wire.Assignment(assigned))
                            output.finished(left=True)
                        started = fixture.clock.monotonic()
                        if service_state == "slow":
                            self.assertEqual(query(), expected)
                            self.assertGreater(fixture.clock.monotonic() - started, 0.5)
                            self.assertEqual(transport.requests[-1], replace(request,
                                code_version=client.LOADED_ADMISSION_CODE_VERSION))
                        else:
                            with self.assertRaises(wire.AdmissionUnavailable) as error:
                                _ = query()
                            self.assertEqual(error.exception.reason,
                                "no socket" if service_state == "missing" else "connect refused")
                            self.assertEqual(fixture.clock.monotonic(), started)
                        if operation == "memory-handoff":
                            self.assertIn("another machine is free after 11 min 45 s: leaving the memory wait",
                                          errors.getvalue())
                    self.assertEqual(len(transport.attempts), len(exchanges) + 1)
                    self.assertEqual(len(transport.requests), len(exchanges) + (service_state == "slow"))
                    self.assertFalse(fixture.clock.sleeps)
                    self.doCleanups()

    def test_placement_control_unreachable_limit_bounds_silence_and_repeated_resets(self) -> None:
        for failure in ("silence", "resets"):
            for limit in (3, 60):
                with self.subTest(failure=failure, limit=limit), AdmissionFixture() as fixture:
                    fixture.configure(unreachable_limit_seconds=limit)
                    exchanges = ([Exchange(TimeoutError())] if failure == "silence" else
                                 [Exchange(TimedChunk(1, b'{"kind":'), ConnectionResetError(errno.ECONNRESET, "reset"))
                                  for _ in range(limit)])
                    transport = self.use_transport(fixture, *exchanges)
                    with redirect_stderr(io.StringIO()) as errors:
                        result = client.main(["cancel-placement", "--episode", "waiting-test",
                            "--pid", str(os.getpid()), "--reason", "another machine is free"])
                    self.assertEqual(result, 2)
                    self.assertEqual(fixture.clock.monotonic(), limit)
                    duration = "1 min" if limit == 60 else f"{limit} s"
                    self.assertIn(f"build queue unreachable for {duration}; step stopped", errors.getvalue())
                    self.assertEqual(len(transport.attempts), len(exchanges))
                    self.assertTrue(transport.requests)
                    self.assertTrue(all(request == transport.requests[0] for request in transport.requests))
                    self.assertFalse(fixture.clock.sleeps)
                    self.doCleanups()

    def test_query_timeout_uses_configuration_for_the_complete_reply(self) -> None:
        with AdmissionFixture() as fixture:
            reply = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(reply, wire.ViewReply)
            encoded = reply_bytes(reply)
            connect, send = ScriptSocket.connect, ScriptSocket.sendall
            for timeout in (0.8, 1.4):
                fixture.configure(query_timeout_seconds=timeout)
                for succeeds in (True, False):
                    with self.subTest(timeout=timeout, succeeds=succeeds):
                        start = fixture.clock.monotonic()
                        last_chunk = TimedChunk(timeout / 4, encoded[-1:]) if succeeds else TimeoutError()
                        transport = self.use_transport(fixture, Exchange(
                            TimedChunk(timeout / 4, encoded[:-1]), last_chunk))

                        def connect_slowly(connection: ScriptSocket, path: str) -> None:
                            connect(connection, path)
                            fixture.clock.advance(timeout / 8)

                        def send_slowly(connection: ScriptSocket, data: bytes) -> None:
                            send(connection, data)
                            fixture.clock.advance(timeout / 8)

                        with mock.patch.object(ScriptSocket, "connect", connect_slowly), \
                             mock.patch.object(ScriptSocket, "sendall", send_slowly):
                            if succeeds:
                                self.assertEqual(client.view(), reply.reading)
                            else:
                                with self.assertRaises(wire.AdmissionUnavailable) as error:
                                    _ = client.view()
                                self.assertEqual(error.exception.reason, f"no answer in {timeout:g} s")
                        self.assertAlmostEqual(fixture.clock.monotonic() - start, timeout * (0.75 if succeeds else 1))
                        self.assertAlmostEqual(transport.timeouts[1], timeout * 7 / 8)
                        self.assertEqual(len(transport.attempts), 1)
                        self.assertFalse(fixture.clock.sleeps)
                        self.doCleanups()

    def test_successful_queries_return_the_domain_record(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            _ = fixture.server.handle_request(fixture.request())
            reply = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(reply, wire.ViewReply)
            reading = reply.reading
            _ = self.use_transport(fixture, Exchange(reply_bytes(reply)),
                Exchange(reply_bytes(wire.ExplainReply(reading.explanation))),
                Exchange(reply_bytes(wire.MineReply(reading))))
            self.assertEqual(client.view(), reading)
            self.assertEqual(client.explain(), reading.explanation)
            self.assertEqual(client.mine("session"), reading)

    def test_estimate_refresh_changes_only_profile_and_freshness(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(since=fixture.clock.now() - 700)
            updated = replace(request, episode="must-not-replace", since=fixture.clock.now(),
                owner=wire.Owner(1, "wrong"), profile=replace(request.profile,
                    peak=records.ExpectedPeak(2 * GIB, "measured", 9)), fresh_test=True)
            transport = self.use_transport(fixture, Exchange(reply_bytes(EXPIRED), after_send={2: (reply_bytes(GRANTED),)}))
            refresh = mock.Mock(return_value=updated)
            self.assertEqual(client.admit(request, lambda _wait: None, refresh), GRANTED)
            self.assertEqual(transport.requests[1], replace(request, profile=updated.profile, fresh_test=True))
            self.assertEqual(refresh.call_count, 1)
            self.assertEqual(len(transport.attempts), 1)

    def test_bad_reply_before_first_wait_refuses_without_retry(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            transport = self.use_transport(fixture, Exchange(b"not JSON\n"))
            result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertIsInstance(result, wire.Refused)
            self.assertEqual(len(transport.attempts), 1)
            self.assertFalse(fixture.clock.sleeps)

    def test_refresh_failures_after_wait_keep_place_until_retry_limit(self) -> None:
        for setting, expected in (("6", 6), ("6.5", 60)):
            with self.subTest(setting=setting), AdmissionFixture() as fixture:
                fixture.hold_starts()
                request = fixture.request()
                server = fixture.server
                handle = server.handle_request
                send = server._send  # pyright: ignore[reportPrivateUsage]
                sent: list[wire.AdmitRequest] = []
                retained: list[service.Connection] = []

                def observe(submitted: wire.Request, connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                            ) -> list[wire.Reply]:
                    if isinstance(submitted, wire.AdmitRequest):
                        sent.append(submitted)
                    return handle(submitted, connection)

                def expire(connection: service.Connection, reply: wire.Reply) -> None:
                    send(connection, EXPIRED if isinstance(reply, wire.Wait) else reply)

                def refresh() -> wire.AdmitRequest:
                    connection = server.waiters[request.episode].connection
                    self.assertIsInstance(connection, service.Connection)
                    assert isinstance(connection, service.Connection)
                    retained.append(connection)
                    raise ValueError("")

                with (mock.patch.dict(os.environ, {"BUILDLOG_MEM_CHECK_RETRY_S": setting}),
                      mock.patch.object(client, "CLOCK", fixture.clock),
                      mock.patch.object(client, "DefaultSelector", side_effect=partial(PrivateSocketSelector, fixture)),
                      mock.patch.object(server, "handle_request", side_effect=observe),
                      mock.patch.object(server, "_send", side_effect=expire), redirect_stderr(io.StringIO()) as output):
                    result = client.admit(request, lambda _wait: None, refresh)
                self.assertEqual(result, wire.Refused("checker returned an error"))
                self.assertEqual(fixture.clock.monotonic(), expected)
                self.assertEqual(sent, [request])
                self.assertIn("memory admission check failed (checker returned an error); trying again, the step keeps its place",
                              output.getvalue())
                self.assertGreater(len(retained), 1)
                self.assertTrue(all(connection is retained[0] for connection in retained))
                self.doCleanups()

    def test_malformed_replies_retry_same_episode_and_valid_wait_resets_failure_clock(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            bad = b"not JSON\n"
            transport = self.use_transport(fixture, Exchange(reply_bytes(WAIT), bad, TimeoutError(), TimedChunk(2, bad),
                reply_bytes(WAIT), TimedChunk(4, bad), reply_bytes(GRANTED)))
            with mock.patch.dict(os.environ, {"BUILDLOG_MEM_CHECK_RETRY_S": "6"}), \
                 redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertGreaterEqual(fixture.clock.monotonic(), 6)
            self.assertTrue(all(value == request for value in transport.requests))
            self.assertEqual(transport.requests, [request])
            self.assertIn("trying again, the step keeps its place", output.getvalue())

    def test_malformed_replies_exhaust_checker_retry_bound(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            _ = self.use_transport(fixture, Exchange(reply_bytes(WAIT), b"bad\n"),
                *(Exchange(TimedChunk(2, b"bad\n")) for _ in range(3)))
            with mock.patch.dict(os.environ, {"BUILDLOG_MEM_CHECK_RETRY_S": "6"}), redirect_stderr(io.StringIO()):
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertIsInstance(result, wire.Refused)
            self.assertGreaterEqual(fixture.clock.monotonic(), 6)

    def test_launch_deadline_includes_missing_socket_silence_and_checker_retries(self) -> None:
        for mode in ("missing", "silent", "checker", "hold"):
            with self.subTest(mode=mode), AdmissionFixture() as fixture:
                request = fixture.launch()
                exchanges = ([Exchange(connect_error=FileNotFoundError()) for _ in range(3)] if mode == "missing" else
                    [Exchange(TimeoutError())] if mode == "silent" else
                    [Exchange(reply_bytes(EXPIRED))] if mode == "checker" else
                    [Exchange(TimedChunk(6, reply_bytes(WAIT)))])
                _ = self.use_transport(fixture, *exchanges)
                refresh = mock.Mock(side_effect=ValueError("broken")) if mode == "checker" else lambda: request
                with mock.patch.dict(os.environ, {"BUILDLOG_MEM_WAIT_LIMIT_S": "6"}), redirect_stderr(io.StringIO()):
                    result = client.launch(request, lambda _wait: None, refresh)
                self.assertEqual(result, wire.Refused("launch wait limit reached without admission"))
                self.assertEqual(fixture.clock.monotonic(), 6)
                self.doCleanups()

    def test_a_coalesced_grant_suppresses_the_wait_callback(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            _ = self.use_transport(fixture, Exchange(reply_bytes(WAIT) + reply_bytes(GRANTED)))
            waits: list[wire.Wait] = []

            self.assertEqual(client.admit(request, waits.append, lambda: request), GRANTED)
            self.assertEqual(waits, [])

    def test_due_leave_hook_yields_to_a_batch_with_a_grant(self) -> None:
        for two_sockets in (False, True):
            with self.subTest(two_sockets=two_sockets), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=2 if two_sockets else 6, running_pass_seconds=3)
                request = fixture.request()
                old = Exchange()
                transport = self.use_transport(fixture, old, Exchange())
                waits: list[wire.Wait] = []

                def make_grant_ready() -> None:
                    if fixture.clock.monotonic() == 3:
                        old.events.append(reply_bytes(WAIT) + reply_bytes(GRANTED))

                transport.on_selector_timeout = make_grant_ready
                with (mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "leave"}),
                      mock.patch.object(client, "_leave_queue", return_value=True) as leave,
                      redirect_stderr(io.StringIO())):
                    result = client.admit(request, waits.append, lambda: request)
                self.assertEqual(result, GRANTED)
                self.assertEqual(waits, [])
                leave.assert_not_called()
                self.assertEqual(transport.requests, [request] * (2 if two_sockets else 1))
                self.doCleanups()

    def test_ready_grant_wins_at_the_launch_deadline(self) -> None:
        for padding in (b"", b" " * 65536):
            with self.subTest(padding=len(padding)), AdmissionFixture() as fixture:
                request = fixture.launch()
                grant = wire.LaunchAdmitted(GIB, "new-source")
                exchange = Exchange()
                transport = self.use_transport(fixture, exchange)
                payload = padding + reply_bytes(grant)
                transport.on_selector_timeout = partial(exchange.events.append, payload)
                with (mock.patch.dict(os.environ, {"BUILDLOG_MEM_WAIT_LIMIT_S": "5", "LINT_QUEUE_LEAVE_HOOK": "",
                                                   "BUILDLOG_MAC_IDLE_HOOK": ""}), redirect_stderr(io.StringIO())):
                    result = client.launch(request, lambda _wait: None, lambda: request)
                self.doCleanups()
                self.assertEqual(result, grant)
                self.assertEqual(fixture.clock.monotonic(), 5)
                self.assertEqual(transport.requests, [request])

    def test_true_leave_hook_releases_the_grant_that_arrives_during_it(self) -> None:
        for padding in (b"", b" " * 65536):
            with self.subTest(padding=len(padding)):
                self.assert_release_after_leave_with_grant(padding)

    def assert_release_after_leave_with_grant(self, padding: bytes) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(running_pass_seconds=3)
            request = fixture.request()
            grant, _replay = self.saved_grant_replay(fixture, request)
            assert isinstance(grant, wire.Admitted)
            exchange = Exchange()
            transport = self.use_transport(fixture, exchange, Exchange(answer=lambda item: fixture.server.handle_request(item)[0]))

            def leave_with_grant() -> bool:
                self.assertIn(request.episode, fixture.server.reservations)
                exchange.events.append(padding + reply_bytes(grant))
                return True

            with (mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "leave"}),
                  mock.patch.object(client, "_leave_queue", side_effect=leave_with_grant) as leave,
                  redirect_stderr(io.StringIO())):
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.doCleanups()
            self.assertEqual(result, client.LeftQueue())
            self.assertNotIn(request.episode, fixture.server.reservations)
            reserve, released = transport.requests
            self.assertEqual(reserve, request)
            assert isinstance(released, wire.ReleaseRequest)
            self.assertEqual(released.reservation, grant.reservation)
            leave.assert_called_once()

    def test_split_grant_wins_over_a_buffered_refusal_in_either_socket_order(self) -> None:
        for refusing_index in (0, 1):
            with self.subTest(refusing_index=refusing_index), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=2, unreachable_limit_seconds=8)
                request = fixture.request()
                pairs = [socket.socketpair(), socket.socketpair()]
                for endpoint, peer in pairs:
                    _ = fixture.enter_context(endpoint)
                    _ = fixture.enter_context(peer)
                    endpoint.setblocking(False)
                    peer.settimeout(1)
                endpoints = [endpoint for endpoint, _peer in pairs]
                refusing = pairs[refusing_index][1]
                granting = pairs[1 - refusing_index][1]
                payload = b" " * 65536 + reply_bytes(GRANTED)
                split = 65541
                sent_refusal = False
                polls_after_refusal = 0

                def send_refusal_and_prefix() -> None:
                    nonlocal sent_refusal
                    if fixture.clock.monotonic() == 4:
                        refusing.sendall(reply_bytes(wire.Refused("request limit changed")))
                        granting.sendall(payload[:split])
                        sent_refusal = True

                def send_suffix_on_next_poll() -> None:
                    nonlocal polls_after_refusal
                    if sent_refusal:
                        polls_after_refusal += 1
                        if polls_after_refusal == 2:
                            granting.sendall(payload[split:])
                            granting.shutdown(socket.SHUT_WR)

                with (mock.patch.object(client, "CLOCK", fixture.clock),
                      mock.patch.object(socket.socket, "connect_ex", return_value=0),
                      mock.patch.object(client, "_acquisition_socket", side_effect=endpoints) as dial,
                      mock.patch.object(client, "DefaultSelector", side_effect=partial(
                          PrivateSocketSelector, fixture, send_refusal_and_prefix, before_read=send_suffix_on_next_poll)),
                      redirect_stderr(io.StringIO()) as errors):
                    result = client.admit(request, lambda _wait: None, lambda: request)
                self.assertEqual(result, GRANTED)
                self.assertNotIn("request limit changed", errors.getvalue())
                self.assertEqual(dial.call_count, 2)
                self.assertEqual([wire.parse_request(peer.recv(65536)) for _endpoint, peer in pairs], [request, request])

    def test_zero_wait_launch_refuses_on_the_first_transport_failure(self) -> None:
        for failure in ("eof", "reset", "connect", "async connect", "send"):
            with self.subTest(failure=failure), AdmissionFixture() as fixture:
                request = fixture.launch()
                failed = {
                    "eof": Exchange(b""),
                    "reset": Exchange(ConnectionResetError(errno.ECONNRESET, "reset")),
                    "connect": Exchange(connect_error=ConnectionRefusedError()),
                    "async connect": Exchange(),
                    "send": Exchange(),
                }[failure]
                transport = self.use_transport(fixture, failed, Exchange(reply_bytes(wire.Refused("retried"))))
                connect_ex = ScriptSocket.connect_ex
                send = ScriptSocket.send

                def start_connect(peer: ScriptSocket, path: str) -> int:
                    result = connect_ex(peer, path)
                    return errno.EINPROGRESS if failure == "async connect" and peer.exchange is failed else result

                def send_request(peer: ScriptSocket, data: bytes) -> int:
                    if failure == "send" and peer.exchange is failed:
                        raise BrokenPipeError(errno.EPIPE, "send failed")
                    return send(peer, data)

                with (mock.patch.dict(os.environ, {"BUILDLOG_MEM_WAIT_LIMIT_S": "0",
                                                   "LINT_QUEUE_LEAVE_HOOK": "", "BUILDLOG_MAC_IDLE_HOOK": ""}),
                      mock.patch.object(ScriptSocket, "connect_ex", autospec=True, side_effect=start_connect),
                      mock.patch.object(ScriptSocket, "getsockopt", return_value=errno.ECONNREFUSED),
                      mock.patch.object(ScriptSocket, "send", autospec=True, side_effect=send_request),
                      redirect_stderr(io.StringIO())):
                    result = client.launch(request, lambda _wait: None, lambda: request)
                self.doCleanups()
                self.assertEqual(result, wire.Refused(client.LAUNCH_WAIT_LIMIT_REASON))
                self.assertEqual(transport.attempts, [0])
                self.assertEqual(transport.selector_timeouts, [])
                self.assertEqual(fixture.clock.monotonic(), 0)

    def test_zero_wait_launch_accepts_a_complete_grant_before_eof(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.launch()
            grant = wire.LaunchAdmitted(GIB, "new-source")
            endpoint, peer = socket.socketpair()
            with endpoint, peer:
                endpoint.setblocking(False)
                peer.setblocking(False)
                received: list[wire.Request] = []

                def reply_and_close() -> None:
                    if received:
                        return
                    try:
                        data = peer.recv(65536)
                    except BlockingIOError:
                        return
                    received.append(wire.parse_request(data))
                    peer.sendall(reply_bytes(grant))
                    peer.shutdown(socket.SHUT_WR)

                with (mock.patch.dict(os.environ, {"BUILDLOG_MEM_WAIT_LIMIT_S": "0",
                                                   "LINT_QUEUE_LEAVE_HOOK": "", "BUILDLOG_MAC_IDLE_HOOK": ""}),
                      mock.patch.object(client, "CLOCK", fixture.clock),
                      mock.patch.object(socket.socket, "connect_ex", return_value=0),
                      mock.patch.object(client, "_acquisition_socket", return_value=endpoint) as dial,
                      mock.patch.object(client, "DefaultSelector", side_effect=partial(
                          PrivateSocketSelector, fixture, before_read=reply_and_close)),
                      redirect_stderr(io.StringIO())):
                    result = client.launch(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, grant)
            self.assertEqual(received, [request])
            dial.assert_called_once()
            self.assertEqual(fixture.clock.monotonic(), 0)

    def test_observer_failure_aborts_and_releases_a_grant_from_the_final_drain(self) -> None:
        for failure_type in (RuntimeError, OSError, conf.Unusable):
            for expires in (False, True):
                with self.subTest(failure=failure_type.__name__, expires=expires), AdmissionFixture() as fixture:
                    request = fixture.service_request()
                    active = Exchange(reply_bytes(WAIT))
                    transport = self.use_transport(fixture, active, Exchange(reply_bytes(wire.Ok())))
                    failure = failure_type("fixture observer failed")
                    waits: list[wire.Wait] = []

                    def observe(wait: wire.Wait) -> None:
                        waits.append(wait)
                        if expires:
                            fixture.clock.advance(5)
                        active.events.append(reply_bytes(GRANTED))
                        raise failure

                    try:
                        with (mock.patch.dict(os.environ, {"BUILDLOG_MEM_CHECK_RETRY_S": "3", "LINT_QUEUE_LEAVE_HOOK": "",
                                                           "BUILDLOG_MAC_IDLE_HOOK": ""}),
                              redirect_stderr(io.StringIO()) as output, self.assertRaises(failure_type) as raised):
                            _ = client.service_admit(request, observe, wire.AcquisitionTiming(10, 1, 5))
                    finally:
                        self.doCleanups()
                    self.assertIs(raised.exception, failure)
                    self.assertEqual(waits, [WAIT])
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual(transport.attempts, [0, 5 if expires else 0])
                    admitted, release = transport.requests
                    self.assertEqual(admitted, request)
                    assert isinstance(release, wire.ReleaseRequest)
                    self.assertEqual((release.reservation, release.status), (GRANTED.reservation, 1))
                    self.assertTrue(all(connection.closed for connection in transport.sockets))

    def test_abort_releases_decoded_grants_and_keeps_the_original_exception(self) -> None:
        for stage, launching, cleanup in (
            ("acquisition", False, "ok"), ("acquisition", True, "ok"),
            ("drain", False, "refusal"), ("drain", True, "ok"),
            ("drain after read", False, "ok"), ("hold check", False, "exception"),
            ("close", False, "ok"),
        ):
            with self.subTest(stage=stage, launching=launching, cleanup=cleanup), AdmissionFixture() as fixture:
                fixture.configure(running_pass_seconds=3)
                request = fixture.launch() if launching else fixture.request()
                grant = wire.LaunchAdmitted(GIB, "new-source") if launching else GRANTED
                abort = SystemExit("stop in hold check") if stage == "hold check" else KeyboardInterrupt(stage)
                initial_reply = WAIT if stage == "acquisition" else None if stage == "drain after read" else grant
                peers, received, dial = self.use_socket_pairs(fixture, initial_reply)
                drain = client.AcquisitionSockets.drain
                close = PrivateSocketSelector.close

                def interrupt_wait(_wait: wire.Wait) -> None:
                    peers[0].sendall(reply_bytes(grant))
                    raise abort

                def interrupt_drain(sockets: client.AcquisitionSockets, item: client.AdmissionRequest) -> None:
                    if stage == "drain" and sockets.terminal() is not None:
                        raise abort
                    drain(sockets, item)
                    if stage == "drain after read" and sockets.terminal() is not None:
                        raise abort

                def leave_after_grant() -> bool:
                    peers[0].sendall(reply_bytes(grant))
                    return True

                def interrupt_close(selector: PrivateSocketSelector) -> None:
                    close(selector)
                    if stage == "close":
                        raise abort

                def admits() -> bool:
                    if stage == "hold check" and received:
                        raise abort
                    return True

                def release_grant(*_args: object) -> wire.Ok | wire.Refused:
                    if cleanup == "exception":
                        raise KeyboardInterrupt("release interrupted")
                    return wire.Refused("release refused") if cleanup == "refusal" else wire.Ok()

                hold = client.AdmissionHoldCheck(lambda: None, admits)
                with (mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "leave" if stage == "drain after read" else "",
                                                   "BUILDLOG_MAC_IDLE_HOOK": ""}),
                      mock.patch.object(client, "_leave_queue", side_effect=leave_after_grant),
                      mock.patch.object(client.AcquisitionSockets, "drain", autospec=True, side_effect=interrupt_drain),
                      mock.patch.object(PrivateSocketSelector, "close", autospec=True, side_effect=interrupt_close),
                      mock.patch.object(client, "release", side_effect=release_grant) as released,
                      redirect_stderr(io.StringIO()), self.assertRaises(BaseException) as raised):
                    if isinstance(request, wire.LaunchRequest):
                        launch_request = request
                        _ = client.launch(launch_request, interrupt_wait, lambda: launch_request)
                    else:
                        admit_request = request
                        _ = client.admit(admit_request, interrupt_wait, lambda: admit_request, _attempt=hold)
                self.assertIs(raised.exception, abort)
                if launching:
                    released.assert_not_called()
                else:
                    released.assert_called_once_with(GRANTED.reservation, 1, GRANTED.cleanup_timing)
                if cleanup != "ok":
                    self.assertIn("release", str(getattr(abort, "__notes__", ())))
                self.assertEqual(received, [request])
                dial.assert_called_once()

    def test_abort_releases_each_distinct_reservation_from_both_sockets(self) -> None:
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=2)
                request = fixture.request()
                second = GRANTED if duplicate else replace(GRANTED, reservation="second")
                peers, received, dial = self.use_socket_pairs(fixture, None, WAIT)
                abort = KeyboardInterrupt("stop acquisition")

                def interrupt_wait(_wait: wire.Wait) -> None:
                    peers[0].sendall(reply_bytes(GRANTED))
                    peers[1].sendall(reply_bytes(second))
                    raise abort

                with (mock.patch.object(client, "release", return_value=wire.Ok()) as released,
                      redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt) as raised):
                    _ = client.admit(request, interrupt_wait, lambda: request)
                self.assertIs(raised.exception, abort)
                grants = [GRANTED] if duplicate else [GRANTED, second]
                self.assertEqual(released.call_args_list,
                                 [mock.call(grant.reservation, 1, grant.cleanup_timing) for grant in grants])
                self.assertEqual(received, [request, request])
                self.assertEqual(dial.call_count, 2)

    def test_leave_and_hold_rejection_require_a_release_acknowledgment(self) -> None:
        for source in ("leave hook", "hold rejection", "hold leave"):
            for cleanup in (wire.Ok(), wire.Refused("release refused"), RuntimeError("release failed")):
                with self.subTest(source=source, cleanup=type(cleanup).__name__), AdmissionFixture() as fixture:
                    fixture.configure(running_pass_seconds=3)
                    request = fixture.request()
                    second = replace(GRANTED, reservation="second")
                    peers, received, dial = self.use_socket_pairs(
                        fixture, None if source == "leave hook" else GRANTED, second)
                    acknowledged = False

                    def leave_with_grant() -> bool:
                        peers[0].sendall(reply_bytes(GRANTED))
                        return True

                    def admits() -> bool:
                        if not received or acknowledged:
                            return True
                        if source == "hold leave":
                            raise client._QueueLeft()  # pyright: ignore[reportPrivateUsage]
                        return False

                    def release_grant(*_args: object) -> wire.Ok | wire.Refused:
                        nonlocal acknowledged
                        if isinstance(cleanup, RuntimeError):
                            raise cleanup
                        acknowledged = isinstance(cleanup, wire.Ok)
                        return cleanup

                    hold = client.AdmissionHoldCheck(lambda: None, admits)
                    with (mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "leave" if source == "leave hook" else "",
                                                       "BUILDLOG_MAC_IDLE_HOOK": ""}),
                          mock.patch.object(client, "_leave_queue", side_effect=leave_with_grant),
                          mock.patch.object(client, "release", side_effect=release_grant) as released,
                          redirect_stderr(io.StringIO())):
                        if isinstance(cleanup, RuntimeError):
                            with self.assertRaises(RuntimeError) as raised:
                                _ = client.admit(request, lambda _wait: None, lambda: request, _attempt=hold)
                            self.assertIs(raised.exception, cleanup)
                        else:
                            result = client.admit(request, lambda _wait: None, lambda: request, _attempt=hold)
                            expected = (cleanup if isinstance(cleanup, wire.Refused)
                                        else second if source == "hold rejection" else client.LeftQueue())
                            self.assertEqual(result, expected)
                    released.assert_called_once_with(GRANTED.reservation, 1, GRANTED.cleanup_timing)
                    self.assertEqual(received[0], request)
                    if source == "hold rejection" and isinstance(cleanup, wire.Ok):
                        self.assertEqual(dial.call_count, 2)
                        later = received[1]
                        assert isinstance(later, wire.AdmitRequest)
                        self.assertNotEqual(later.episode, request.episode)
                        self.assertEqual(later.since, request.since)
                    else:
                        self.assertEqual(dial.call_count, 1)
                        self.assertEqual(received, [request])

    def test_hold_blocks_retry_after_socket_allocation_fails(self) -> None:
        for replacement in (False, True):
            with self.subTest(replacement=replacement), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=2, reconnect_seconds=1, unreachable_limit_seconds=6)
                request = fixture.request()
                failure_at = 2 if replacement else 0
                exchanges = [Exchange()] if replacement else []
                transport = self.use_transport(fixture, *exchanges, Exchange(reply_bytes(GRANTED)))
                allocation = client._acquisition_socket  # pyright: ignore[reportPrivateUsage]
                allocations: list[float] = []
                checks: list[float] = []

                def allocate() -> socket.socket:
                    now = fixture.clock.monotonic()
                    allocations.append(now)
                    if now == failure_at:
                        raise OSError(errno.EMFILE, "allocation failed")
                    return allocation()

                def admits() -> bool:
                    now = fixture.clock.monotonic()
                    checks.append(now)
                    return now != failure_at + 1 or len(transport.requests) > int(replacement)

                with (mock.patch.object(client, "_acquisition_socket", side_effect=allocate),
                      redirect_stderr(io.StringIO())):
                    result = client.admit(request, lambda _wait: None, lambda: request,
                                          _attempt=client.AdmissionHoldCheck(lambda: None, admits))
                self.doCleanups()
                self.assertEqual(result, GRANTED)
                self.assertEqual(allocations, [0, 2, 4] if replacement else [0, 2])
                self.assertIn(failure_at + 1, checks)
                self.assertEqual(transport.requests, [request] * (2 if replacement else 1))
                self.assertEqual(fixture.clock.monotonic(), failure_at + 2)

    def test_scripted_selector_exposes_reads_until_the_next_timed_event(self) -> None:
        for reverse in (False, True):
            with self.subTest(reverse=reverse), AdmissionFixture() as fixture:
                burst = b"x" * 65540
                reset = ConnectionResetError(errno.ECONNRESET, "reset")
                transport = ScriptedTransport(fixture.clock,
                    Exchange(b"first", b"second", burst, TimedChunk(2, b"suffix"), b""), Exchange(reset))
                first, second = transport.socket(), transport.socket()
                request = wire.encode_request(wire.BuildHistoryRequest()).encode()
                peers = (second, first) if reverse else (first, second)
                with transport.selector() as selector:
                    for peer in peers:
                        selector.register(peer, EVENT_READ)
                        peer.sendall(request)
                    self.assertEqual([key.fileobj for key, _mask in selector.select(0)], list(peers))
                    with self.assertRaises(ConnectionResetError):
                        _ = second.recv(65536)
                    for data in (b"first", b"second", burst[:65536], burst[65536:]):
                        self.assertEqual([key.fileobj for key, _mask in selector.select(0)], [first])
                        self.assertEqual(first.recv(65536), data)
                    self.assertEqual(selector.select(0), [])
                    self.assertEqual(fixture.clock.monotonic(), 0)
                    self.assertEqual([key.fileobj for key, _mask in selector.select(2)], [first])
                    self.assertEqual(first.recv(65536), b"suffix")
                    self.assertEqual(fixture.clock.monotonic(), 2)
                    self.assertEqual([key.fileobj for key, _mask in selector.select(0)], [first])
                    self.assertEqual(first.recv(65536), b"")
                    self.assertEqual(selector.select(0), [])

    def test_timed_selector_uses_the_earliest_ready_event(self) -> None:
        with AdmissionFixture() as fixture:
            transport = ScriptedTransport(fixture.clock,
                Exchange(TimedChunk(2, b"abc")), Exchange(TimedChunk(3, b"later")))
            first, second = transport.socket(), transport.socket()
            request = wire.encode_request(wire.BuildHistoryRequest()).encode()
            with transport.selector() as selector:
                for peer in (first, second):
                    peer.setblocking(False)
                    peer.sendall(request)
                    selector.register(peer, EVENT_READ)
                selector.modify(second, EVENT_READ | EVENT_WRITE)
                self.assertEqual([mask for _key, mask in selector.select(9)], [EVENT_WRITE])
                self.assertEqual(fixture.clock.monotonic(), 0)
                selector.modify(second, EVENT_READ)
                self.assertEqual(selector.select(0), [])
                self.assertEqual(selector.select(1), [])
                self.assertEqual(fixture.clock.monotonic(), 1)
                with self.assertRaises(BlockingIOError):
                    _ = first.recv(1)
                self.assertEqual(fixture.clock.monotonic(), 1)
                self.assertEqual([key.fileobj for key, _mask in selector.select(1)], [first])
                self.assertEqual(first.recv(1), b"a")
                self.assertEqual([key.fileobj for key, _mask in selector.select(0)], [first])
                self.assertEqual(first.recv(9), b"bc")
                self.assertEqual(fixture.clock.monotonic(), 2)
                self.assertEqual([key.fileobj for key, _mask in selector.select(9)], [second])
                self.assertEqual(second.recv(9), b"later")
                self.assertEqual(fixture.clock.monotonic(), 3)

    def test_selection_abort_releases_grants_and_closes_sockets(self) -> None:
        for launch in (False, True):
            with self.subTest(launch=launch), AdmissionFixture() as fixture:
                request = fixture.launch() if launch else fixture.request()
                grant = wire.LaunchAdmitted(GIB, "new-source") if launch else GRANTED
                peers, received, _dial = self.use_socket_pairs(fixture, grant)
                abort = KeyboardInterrupt("select grant")
                select_reply = client.AcquisitionSockets.terminal
                selections = 0

                def interrupt_selection(sockets: client.AcquisitionSockets) -> client.AdmissionReply | None:
                    nonlocal selections
                    reply = select_reply(sockets)
                    if reply is not None:
                        selections += 1
                        if selections == 2:
                            raise abort
                    return reply

                with (mock.patch.object(client.AcquisitionSockets, "terminal", interrupt_selection),
                      mock.patch.object(client, "release", return_value=wire.Ok()) as released,
                      self.assertRaises(KeyboardInterrupt) as raised):
                    if isinstance(request, wire.LaunchRequest):
                        item = request
                        _ = client.launch(item, lambda _wait: None, lambda: item)
                    else:
                        admission = request
                        _ = client.admit(admission, lambda _wait: None, lambda: admission)
                self.assertIs(raised.exception, abort)
                self.assertEqual(received, [request])
                self.assertEqual(peers[0].recv(1), b"")
                if launch:
                    released.assert_not_called()
                else:
                    released.assert_called_once_with(GRANTED.reservation, 1, GRANTED.cleanup_timing)

    def test_configuration_diagnostics_run_before_any_grant(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            transport = self.use_transport(fixture, Exchange(reply_bytes(GRANTED)))
            abort = KeyboardInterrupt("configuration diagnostics")
            with (mock.patch.object(records, "warn_configuration_diagnostics", side_effect=abort),
                  self.assertRaises(KeyboardInterrupt) as raised):
                _ = client.admit(request, lambda _wait: None, lambda: request)
            self.assertIs(raised.exception, abort)
            self.assertEqual(transport.attempts, [])
            self.assertEqual(transport.requests, [])

    def test_local_placement_diagnostics_precede_request(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import local_candidate, placement_request

        for failure in (BrokenPipeError("diagnostic output closed"), KeyboardInterrupt("write interrupted"), None):
            with self.subTest(failure=failure), AdmissionFixture() as fixture:
                initial = placement_request(fixture)
                candidate = local_candidate(fixture)
                request = replace(initial, constraints=replace(initial.constraints, candidates=(candidate,)))
                assignment = placement.PlacementAssignment(request.episode, "local-attempt", candidate.destination,
                    request.constraints.source_key, request.owner, request.constraints.session, request.since)
                grant = wire.LocalPlacementGranted(assignment, GRANTED)
                diagnostic = records.ConfigurationDiagnostic("warning: private placement configuration")
                configuration = replace(records.read_memory_config(report=False), diagnostics=(diagnostic,))
                transport = self.use_transport(fixture, Exchange(reply_bytes(grant)))
                warn = records.warn_configuration_diagnostics
                request_counts: list[int] = []

                def warn_before_request(diagnostics: tuple[records.ConfigurationDiagnostic, ...]) -> None:
                    request_counts.append(len(transport.requests))
                    self.assertEqual(diagnostics, (diagnostic,))
                    if failure is not None:
                        raise failure
                    warn(diagnostics)

                try:
                    with (mock.patch.object(records, "read_memory_config", return_value=configuration),
                          mock.patch.object(records, "_warned_configuration_diagnostics", set[records.ConfigurationDiagnostic]()),
                          mock.patch.object(records, "warn_configuration_diagnostics", warn_before_request),
                          redirect_stderr(io.StringIO()) as output):
                        if failure is not None:
                            with self.assertRaises(type(failure)) as raised:
                                _ = client.request_placement(request)
                            self.assertIs(raised.exception, failure)
                        else:
                            self.assertEqual(client.request_placement(request), grant)
                finally:
                    self.doCleanups()

                self.assertEqual(request_counts, [0])
                if failure is not None:
                    self.assertEqual(transport.attempts, [])
                    self.assertEqual(transport.requests, [])
                else:
                    self.assertEqual(transport.attempts, [0])
                    self.assertEqual(transport.requests, [request])
                    self.assertEqual(output.getvalue(), diagnostic.diagnostic + "\n")

    def test_second_interrupt_during_socket_setup_stops_cleanup(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(silence_seconds=2)
            request = fixture.request()
            endpoints: list[socket.socket] = []
            _peers, received, dial = self.use_socket_pairs(fixture, None, None, endpoints=endpoints)
            first = KeyboardInterrupt("register replacement")
            second = KeyboardInterrupt("close replacement")
            register = PrivateSocketSelector.register
            close = socket.socket.close
            close_selector = PrivateSocketSelector.close
            selectors: list[PrivateSocketSelector] = []

            def interrupt_registration(selector: PrivateSocketSelector, endpoint: socket.socket, events: int
                                       ) -> SelectorKey:
                if endpoint is endpoints[1]:
                    selectors.append(selector)
                    raise first
                return register(selector, endpoint, events)

            def interrupt_close(endpoint: socket.socket) -> None:
                was_open = endpoint.fileno() >= 0
                close(endpoint)
                if was_open and endpoint is endpoints[1]:
                    raise second

            try:
                with (mock.patch.object(PrivateSocketSelector, "register", interrupt_registration),
                      mock.patch.object(PrivateSocketSelector, "close", autospec=True, side_effect=close_selector) as closed,
                      mock.patch.object(socket.socket, "close", interrupt_close),
                      mock.patch.object(client, "release", return_value=wire.Ok()) as released,
                      redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt) as raised):
                    _ = client.admit(request, lambda _wait: None, lambda: request)
                self.assertIs(raised.exception, second)
                self.assertEqual(received, [request])
                self.assertEqual(dial.call_count, 2)
                self.assertEqual(endpoints[1].fileno(), -1)
                self.assertGreaterEqual(endpoints[0].fileno(), 0)
                closed.assert_not_called()
                released.assert_not_called()
            finally:
                for selector in selectors:
                    close_selector(selector)

    def test_grant_registry_survives_reply_queue_removal(self) -> None:
        for reverse in (False, True):
            for duplicate in (False, True):
                with self.subTest(reverse=reverse, duplicate=duplicate), AdmissionFixture() as fixture:
                    fixture.configure(silence_seconds=2)
                    request = fixture.request()
                    other = GRANTED if duplicate else replace(GRANTED, reservation="other")
                    grants = (other, GRANTED) if reverse else (GRANTED, other)
                    peers, received, _dial = self.use_socket_pairs(fixture, None, WAIT)
                    select_reply = client.AcquisitionSockets.terminal
                    selections = 0

                    def finish_selection(sockets: client.AcquisitionSockets) -> client.AdmissionReply | None:
                        nonlocal selections
                        reply = select_reply(sockets)
                        if isinstance(reply, wire.Admitted):
                            selections += 1
                            if selections == 2:
                                for stream in sockets.streams.values():
                                    stream.replies.clear()
                        return reply

                    def send_grants(_wait: wire.Wait) -> None:
                        for peer, grant in zip(peers, grants, strict=True):
                            peer.sendall(reply_bytes(grant))

                    with (mock.patch.object(client.AcquisitionSockets, "terminal", finish_selection),
                          mock.patch.object(client, "release", return_value=wire.Ok()) as released,
                          redirect_stderr(io.StringIO())):
                        result = client.admit(request, send_grants, lambda: request)
                    self.assertEqual(result, grants[1])
                    self.assertEqual(received, [request, request])
                    self.assertEqual([peer.recv(1) for peer in peers], [b"", b""])
                    if duplicate:
                        released.assert_not_called()
                    else:
                        released.assert_called_once_with(grants[0].reservation, 1, grants[0].cleanup_timing)

    def test_cleanup_continues_after_a_failure_and_stops_after_second_interrupt(self) -> None:
        for stage in ("unregister", "socket close", "selector close", "release", "second interrupt"):
            with self.subTest(stage=stage), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=2)
                request = fixture.request()
                endpoints: list[socket.socket] = []
                peers, received, _dial = self.use_socket_pairs(fixture, None, WAIT, endpoints=endpoints)
                second_grant = replace(GRANTED, reservation="other")
                first = KeyboardInterrupt("first interrupt") if stage == "second interrupt" else RuntimeError(stage)
                second = KeyboardInterrupt("second interrupt")
                unregister = PrivateSocketSelector.unregister
                close = socket.socket.close
                selector_close = PrivateSocketSelector.close
                releases: list[str] = []

                def unregister_peer(selector: PrivateSocketSelector, peer: socket.socket) -> SelectorKey:
                    result = unregister(selector, peer)
                    if stage == "unregister" and peer is endpoints[0]:
                        raise first
                    return result

                def close_peer(peer: socket.socket) -> None:
                    was_open = peer.fileno() >= 0
                    close(peer)
                    if was_open and stage == "socket close" and peer is endpoints[0]:
                        raise first

                def close_selector(selector: PrivateSocketSelector) -> None:
                    selector_close(selector)
                    if stage == "selector close":
                        raise first

                def fail_release(reservation: str, _status: int, _timing: wire.CleanupTiming) -> wire.Ok:
                    releases.append(reservation)
                    if len(releases) == 1:
                        if stage == "release":
                            raise first
                        if stage == "second interrupt":
                            raise second
                    return wire.Ok()

                def send_grants(_wait: wire.Wait) -> None:
                    peers[0].sendall(reply_bytes(GRANTED))
                    peers[1].sendall(reply_bytes(second_grant))
                    if stage == "second interrupt":
                        raise first

                with (mock.patch.object(PrivateSocketSelector, "unregister", unregister_peer),
                      mock.patch.object(socket.socket, "close", close_peer),
                      mock.patch.object(PrivateSocketSelector, "close", close_selector),
                      mock.patch.object(client, "release", fail_release),
                      redirect_stderr(io.StringIO()), self.assertRaises(BaseException) as raised):
                    _ = client.admit(request, send_grants, lambda: request)
                self.assertIs(raised.exception, second if stage == "second interrupt" else first)
                self.assertEqual([peer.recv(1) for peer in peers], [b"", b""])
                self.assertEqual(received, [request, request])
                if stage == "second interrupt":
                    self.assertEqual(len(releases), 1)
                else:
                    self.assertCountEqual(releases, [GRANTED.reservation, second_grant.reservation])

    def test_first_hold_rejection_and_late_acceptance_never_connect(self) -> None:
        for accepts_late in (False, True):
            with self.subTest(accepts_late=accepts_late), AdmissionFixture() as fixture:
                fixture.configure(reconnect_seconds=1, unreachable_limit_seconds=3)
                request = fixture.request()
                transport = self.use_transport(fixture,
                    Exchange(connect_error=FileNotFoundError()), Exchange(reply_bytes(GRANTED)))
                checks: list[float] = []

                def admits() -> bool:
                    checks.append(fixture.clock.monotonic())
                    if len(checks) == 1:
                        self.assertEqual(transport.attempts, [])
                        return False
                    if accepts_late and len(checks) == 3:
                        fixture.clock.advance(3)
                    return accepts_late

                hold = client.AdmissionHoldCheck(lambda: None, admits)
                retry = client.AcquisitionRetry(float("inf") if accepts_late else 3)
                try:
                    with (mock.patch.object(client, "AcquisitionRetry", return_value=retry),
                          redirect_stderr(io.StringIO())):
                        result = client.admit(request, lambda _wait: None, lambda: request, _attempt=hold)
                finally:
                    self.doCleanups()
                reason = ("build queue unreachable for 3 s; step stopped; cargo token given back"
                          if accepts_late else client.LAUNCH_WAIT_LIMIT_REASON)
                self.assertEqual(result, wire.Refused(reason))
                self.assertEqual(checks, [0, 1, 2])
                self.assertEqual(transport.attempts, [1] if accepts_late else [])
                self.assertEqual(transport.requests, [])

    def test_deadline_crossing_callbacks_do_not_queue_or_send_a_request(self) -> None:
        for stage in ("attempt", "leave", "wait", "refresh", "history", "diagnostics", "silence", "checker notice",
                      "checker refresh", "checker history", "unreachable refresh", "unreachable history"):
            with self.subTest(stage=stage), AdmissionFixture() as fixture:
                fixture.configure(reconnect_seconds=1, running_pass_seconds=1, silence_seconds=2, unreachable_limit_seconds=5)
                request = fixture.request()
                first = Exchange() if stage in ("attempt", "leave", "silence") else Exchange(reply_bytes(EXPIRED))
                transport = self.use_transport(fixture, first, Exchange(reply_bytes(GRANTED)))
                modify = ScriptedSelector.modify
                report = client.CheckerRetry.report
                checker_limit = stage in ("checker refresh", "checker history")
                unreachable_limit = stage.startswith("unreachable")
                retry = client.AcquisitionRetry(20 if checker_limit or unreachable_limit else 5)
                queued: list[int] = []
                waits: list[wire.Wait] = []
                refreshed: list[float] = []

                def track_writes(selector: ScriptedSelector, peer: ScriptSocket, events: int) -> None:
                    if events & EVENT_WRITE:
                        queued.append(peer.send_count)
                    modify(selector, peer, events)

                def cross_deadline() -> None:
                    fixture.clock.advance(6)

                def leave() -> bool:
                    cross_deadline()
                    return False

                def wait(reply: wire.Wait) -> None:
                    waits.append(reply)
                    if stage == "wait":
                        cross_deadline()
                    if unreachable_limit:
                        retry.unreachable_deadline = fixture.clock.monotonic() + 5

                def refresh() -> wire.AdmitRequest:
                    refreshed.append(fixture.clock.monotonic())
                    if (checker_limit or stage == "checker notice") and len(refreshed) == 1:
                        raise ValueError("broken estimate")
                    cross_deadline()
                    if stage.endswith("history"):
                        _ = client.build_history_generation()
                    return request

                def diagnostics(_diagnostics: object) -> None:
                    if stage == "diagnostics" and transport.requests:
                        cross_deadline()

                def checker_notice(checker: client.CheckerRetry, interval: float) -> None:
                    report(checker, interval)
                    if stage == "checker notice":
                        cross_deadline()

                def silence_notice(_last_valid: float) -> None:
                    cross_deadline()

                try:
                    with (mock.patch.object(client, "AcquisitionRetry", return_value=retry),
                          mock.patch.object(ScriptedSelector, "modify", track_writes),
                          mock.patch.object(records, "warn_configuration_diagnostics", diagnostics),
                          mock.patch.object(client, "_leave_queue", leave),
                          mock.patch.object(client, "_silence_notice", silence_notice),
                          mock.patch.object(client.CheckerRetry, "report", checker_notice),
                          mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "leave" if stage == "leave" else "",
                                                       "BUILDLOG_MAC_IDLE_HOOK": "", "BUILDLOG_MEM_CHECK_RETRY_S": "2"}),
                          redirect_stderr(io.StringIO())):
                        result = client.admit(request, wait, refresh,
                            _attempt=cross_deadline if stage == "attempt" else lambda: None)
                finally:
                    self.doCleanups()
                reason = ("build queue unreachable for 5 s; step stopped; cargo token given back") if unreachable_limit else client.LAUNCH_WAIT_LIMIT_REASON
                self.assertEqual(result, wire.Refused("broken estimate" if checker_limit else reason))
                self.assertEqual(queued, [])
                self.assertEqual(transport.attempts, [] if stage == "attempt" else [0])
                self.assertEqual(transport.requests, [] if stage == "attempt" else [request])
                if stage == "diagnostics":
                    self.assertEqual(waits, [])
                if stage == "checker notice":
                    self.assertEqual(refreshed, [0])

    def test_deadline_stops_deferred_writes_on_private_sockets(self) -> None:
        for stage in ("selector", "connected", "partial"):
            with self.subTest(stage=stage), AdmissionFixture() as fixture:
                request = fixture.launch()
                endpoint, peer = socket.socketpair()
                _ = fixture.enter_context(endpoint)
                _ = fixture.enter_context(peer)
                endpoint.setblocking(False)
                peer.setblocking(False)
                send = socket.socket.send
                select = PrivateSocketSelector.select
                wrote_prefix = False

                def select_write(selector: PrivateSocketSelector, timeout: float | None = None
                                 ) -> list[tuple[SelectorKey, int]]:
                    ready = select(selector, timeout)
                    if (stage == "selector" or stage == "partial" and wrote_prefix and timeout != 0) and any(
                            key.fileobj is endpoint and mask & EVENT_WRITE for key, mask in ready):
                        fixture.clock.advance(6)
                    return ready

                def connected(_endpoint: socket.socket, _level: int, _option: int) -> int:
                    fixture.clock.advance(6)
                    return 0

                def write(connection: socket.socket, data: bytes) -> int:
                    nonlocal wrote_prefix
                    if stage == "partial" and connection is endpoint and not wrote_prefix:
                        wrote_prefix = True
                        return send(connection, data[:7])
                    return send(connection, data)

                with (mock.patch.object(client, "CLOCK", fixture.clock),
                      mock.patch.object(client, "_acquisition_socket", return_value=endpoint),
                      mock.patch.object(client, "DefaultSelector", side_effect=partial(PrivateSocketSelector, fixture)),
                      mock.patch.object(PrivateSocketSelector, "select", select_write),
                      mock.patch.object(socket.socket, "connect_ex", return_value=errno.EINPROGRESS if stage == "connected" else 0),
                      mock.patch.object(socket.socket, "getsockopt", connected),
                      mock.patch.object(socket.socket, "send", write),
                      mock.patch.dict(os.environ, {"BUILDLOG_MEM_WAIT_LIMIT_S": "5", "LINT_QUEUE_LEAVE_HOOK": "",
                                                   "BUILDLOG_MAC_IDLE_HOOK": ""}), redirect_stderr(io.StringIO())):
                    result = client.launch(request, lambda _wait: None, lambda: request)
                self.assertEqual(result, wire.Refused(client.LAUNCH_WAIT_LIMIT_REASON))
                self.assertEqual(endpoint.fileno(), -1)
                expected = wire.encode_request(request).encode()[:7] if stage == "partial" else b""
                self.assertEqual(peer.recv(65536), expected)
                self.assertEqual(peer.recv(1), b"")

    def test_control_request_does_not_write_after_slow_connection(self) -> None:
        with AdmissionFixture() as fixture:
            endpoint, peer = socket.socketpair()
            _ = fixture.enter_context(endpoint)
            _ = fixture.enter_context(peer)
            peer.setblocking(False)

            def slow_connect(_endpoint: socket.socket, _path: str) -> None:
                fixture.clock.advance(6)

            retry = client.AcquisitionRetry(5)
            with (mock.patch.object(client, "CLOCK", fixture.clock),
                  mock.patch.object(client, "AcquisitionRetry", return_value=retry),
                  mock.patch.object(socket.socket, "connect", slow_connect),
                  mock.patch("scripts.build_queue.admission_client.socket.socket", return_value=endpoint),
                  self.assertRaisesRegex(client.CheckerFailure, client.LAUNCH_WAIT_LIMIT_REASON)):
                _ = client.build_history_generation()
            self.assertEqual(endpoint.fileno(), -1)
            self.assertEqual(peer.recv(65536), b"")

    def test_blocking_writes_limit_timeout_after_encoding(self) -> None:
        from scripts.build_queue import admission_wire

        for operation in ("query", "launch history"):
            for stage in ("ready", "backpressure", "expired during encoding"):
                with self.subTest(operation=operation, stage=stage), AdmissionFixture() as fixture:
                    fixture.configure(query_timeout_seconds=5, reconnect_seconds=5, silence_seconds=5)
                    endpoint, peer = socket.socketpair()
                    _ = fixture.enter_context(endpoint)
                    _ = fixture.enter_context(peer)
                    peer.setblocking(False)
                    view, = fixture.server.handle_request(wire.ViewRequest())
                    assert isinstance(view, wire.ViewReply)
                    launch = fixture.launch()
                    grant = wire.LaunchAdmitted(GIB, "new-source")
                    if operation == "launch history":
                        _ = self.use_socket_pairs(fixture, grant)

                    sendall = socket.socket.sendall
                    encode_request = wire.encode_request
                    write_timeouts: list[float] = []
                    sent_request: wire.Request = wire.ViewRequest() if operation == "query" else wire.BuildHistoryRequest()
                    expected = encode_request(replace(sent_request, code_version=client.LOADED_ADMISSION_CODE_VERSION))

                    def slow_encode(request: wire.Request) -> str:
                        if isinstance(request, (wire.ViewRequest, wire.BuildHistoryRequest)):
                            fixture.clock.advance(5 if stage == "expired during encoding" else 4)
                        return encode_request(request)

                    def connect(timeout: float) -> socket.socket:
                        endpoint.settimeout(timeout)
                        return endpoint

                    def write_when_ready(connection: socket.socket, data: bytes) -> None:
                        if connection is not endpoint:
                            sendall(connection, data)
                            return
                        timeout = connection.gettimeout()
                        assert timeout is not None
                        write_timeouts.append(timeout)
                        delay = 2 if stage == "backpressure" else 0.5
                        if timeout < delay:
                            fixture.clock.advance(timeout)
                            raise TimeoutError("private write timed out")
                        fixture.clock.advance(delay)
                        sendall(connection, data)
                        if stage == "ready":
                            reply = view if operation == "query" else wire.BuildHistoryReply(7)
                            sendall(peer, reply_bytes(reply))

                    def refresh() -> wire.LaunchRequest:
                        self.assertEqual(client.build_history_generation(), records.BuildHistoryGeneration(7))
                        return launch

                    with (mock.patch.object(client, "CLOCK", fixture.clock),
                          mock.patch.object(client, "_connect", connect),
                          mock.patch.object(wire, "encode_request", slow_encode),
                          mock.patch.object(admission_wire, "encode_request", slow_encode),
                          mock.patch.object(socket.socket, "sendall", write_when_ready),
                          mock.patch.dict(os.environ, {"BUILDLOG_MEM_WAIT_LIMIT_S": "5", "LINT_QUEUE_LEAVE_HOOK": "",
                                                       "BUILDLOG_MAC_IDLE_HOOK": ""}), redirect_stderr(io.StringIO())):
                        if operation == "launch history":
                            result = client.wait_for_launch(refresh, lambda _wait: None)
                            self.assertEqual(result, grant if stage == "ready" else wire.Refused(client.LAUNCH_WAIT_LIMIT_REASON))
                        elif stage == "ready":
                            self.assertEqual(client.view(), view.reading)
                        else:
                            with self.assertRaises(wire.AdmissionUnavailable) as raised:
                                _ = client.view()
                            self.assertEqual(raised.exception.reason, "no answer in 5 s")

                    self.assertEqual(endpoint.fileno(), -1)
                    self.assertEqual(peer.recv(65536), expected.encode() if stage == "ready" else b"")
                    self.assertEqual(peer.recv(1), b"")
                    self.assertEqual(write_timeouts, [] if stage == "expired during encoding" else [1])
                    self.assertEqual(fixture.clock.monotonic(), 4.5 if stage == "ready" else 5)

    def test_grant_and_cleanup_precedence_at_expiry(self) -> None:
        for action in ("accept", "reject", "release refusal", "leave", "abort", "checker rejects"):
            with self.subTest(action=action), AdmissionFixture() as fixture:
                fixture.configure(reconnect_seconds=1, running_pass_seconds=1)
                request = fixture.request()
                active = Exchange(reply_bytes(EXPIRED)) if action in ("abort", "checker rejects") else (
                    Exchange() if action == "leave" else Exchange(reply_bytes(GRANTED)))
                cleanup = wire.Refused("release refused") if action == "release refusal" else wire.Ok()
                transport = self.use_transport(fixture, active, Exchange(reply_bytes(cleanup)),
                    Exchange(reply_bytes(GRANTED)))
                abort = KeyboardInterrupt("grant arrives during interrupted callback")

                def accepts() -> bool:
                    if transport.requests:
                        fixture.clock.advance(6)
                        return action == "accept"
                    return True

                def leave() -> bool:
                    fixture.clock.advance(6)
                    active.events.append(reply_bytes(GRANTED))
                    return True

                def wait(_wait: wire.Wait) -> None:
                    if action == "abort":
                        _ = leave()
                        raise abort

                def refresh() -> wire.AdmitRequest:
                    raise ValueError("estimate failed")

                if action == "checker rejects":
                    transport.on_selector_timeout = lambda: active.events.append(reply_bytes(GRANTED))
                retry = client.AcquisitionRetry(20 if action == "checker rejects" else 5)
                cleanup_retry = client.AcquisitionRetry(float("inf"))
                hold = client.AdmissionHoldCheck(lambda: None, accepts)
                try:
                    with (mock.patch.object(client, "AcquisitionRetry", side_effect=[retry, cleanup_retry]),
                          mock.patch.object(client, "_leave_queue", leave),
                          mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "leave" if action == "leave" else "",
                                                       "BUILDLOG_MAC_IDLE_HOOK": "", "BUILDLOG_MEM_CHECK_RETRY_S": "2"}),
                          redirect_stderr(io.StringIO())):
                        if action == "abort":
                            with self.assertRaises(KeyboardInterrupt) as raised:
                                _ = client.admit(request, wait, refresh, _attempt=hold)
                            self.assertIs(raised.exception, abort)
                        else:
                            expected = (GRANTED if action == "accept" else client.LeftQueue() if action == "leave" else
                                        cleanup if action == "release refusal" else wire.Refused("estimate failed")
                                        if action == "checker rejects" else wire.Refused(client.LAUNCH_WAIT_LIMIT_REASON))
                            self.assertEqual(client.admit(request, wait, refresh, _attempt=hold), expected)
                finally:
                    self.doCleanups()
                if action == "accept":
                    self.assertEqual(transport.requests, [request])
                else:
                    reserve, release = transport.requests
                    self.assertEqual(reserve, request)
                    self.assertIsInstance(release, wire.ReleaseRequest)
                    assert isinstance(release, wire.ReleaseRequest)
                    self.assertEqual(release.reservation, GRANTED.reservation)
                    self.assertGreaterEqual(transport.attempts[-1], 2 if action == "checker rejects" else 5)
                self.assertTrue(all(peer.closed for peer in transport.sockets))

    def test_failed_history_recovers_on_the_replacement_and_keeps_saved_order(self) -> None:
        for failure in ("negative", "malformed", "silent"):
            with self.subTest(failure=failure), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=5, reconnect_seconds=1, unreachable_limit_seconds=8)
                fixture.free(50 * GIB)
                fixture.hold_starts()
                older = fixture.request(age=20, need=40 * GIB, step="build")
                younger = fixture.request(need=40 * GIB)
                self.assertIsInstance(fixture.connect(younger).replies()[0], wire.Wait)
                server = fixture.server
                send = server._send  # pyright: ignore[reportPrivateUsage]
                handle = server.handle_request
                histories: list[service.Connection] = []
                senders: list[service.Connection] = []
                order_times: list[float] = []
                assert isinstance(older.profile, records.StepProfile)
                previous = older.profile

                def expire_head(connection: service.Connection, reply: wire.Reply) -> None:
                    if isinstance(reply, wire.Wait) and connection.request == older:
                        if len(senders) < 2 and all(peer is not connection for peer in senders):
                            senders.append(connection)
                            send(connection, EXPIRED)
                        return
                    send(connection, reply)

                def observe(item: wire.Request, peer: service.Connection | service.NoConnection = service.NO_CONNECTION
                            ) -> list[wire.Reply]:
                    if isinstance(item, wire.BuildHistoryRequest):
                        assert isinstance(peer, service.Connection)
                        waiter = server.waiters[older.episode]
                        self.assertIs(waiter.connection, peer)
                        self.assertEqual(fixture.queue_pids(), [older.owner.pid, younger.owner.pid])
                        order_times.append(waiter.row.get("order_since", waiter.row["since"]))
                        histories.append(peer)
                        if len(histories) == 1:
                            if failure == "malformed":
                                peer.socket.sendall(b"invalid history\n")
                            return [wire.BuildHistoryReply(-1)] if failure == "negative" else []
                        self.assertIsNot(peer, histories[0], "a failed lookup reused the old socket")
                        server.build_history_generation = 7
                        return [wire.BuildHistoryReply(7)]
                    if isinstance(item, wire.AdmitRequest) and item.episode == older.episode and item != older:
                        self.assertEqual((item.owner, item.since), (older.owner, older.since))
                        self.assertIs(server.waiters[older.episode].connection, peer)
                        write_pressure_reading(fixture.root / "pressure")
                    return handle(item, peer)

                def refresh() -> wire.AdmitRequest:
                    generation = client.build_history_generation()
                    return replace(older, profile=replace(previous, build_history_generation=generation,
                                                         estimated_at=fixture.clock.now()))

                with (mock.patch.object(client, "CLOCK", fixture.clock),
                      mock.patch.object(client, "DefaultSelector", side_effect=partial(PrivateSocketSelector, fixture)),
                      mock.patch.object(server, "_send", side_effect=expire_head),
                      mock.patch.object(server, "handle_request", side_effect=observe),
                      mock.patch.dict(os.environ, {"BUILDLOG_MEM_CHECK_RETRY_S": "10"}),
                      redirect_stderr(io.StringIO())):
                    result = client.admit(older, lambda _wait: None, refresh)
                self.assertIsInstance(result, wire.Admitted)
                self.assertEqual(histories, senders)
                self.assertEqual(order_times, [older.since, older.since])
                self.assertEqual(fixture.queue_pids(), [younger.owner.pid])
                self.assertEqual(set(server.reservations), {older.episode})
                saved = service.read_state(fixture.state_dir)
                self.assertEqual(saved.ledger[older.episode].reply, result)
                self.assertEqual(saved.reservations[older.episode].queue_waiter.get("order_since"), older.since)

    def test_replacement_wait_precedes_the_next_history_request(self) -> None:
        class HistoryBeforeWait(BaseException):
            pass

        with AdmissionFixture() as fixture:
            fixture.configure(silence_seconds=5, reconnect_seconds=1, unreachable_limit_seconds=8)
            request = fixture.request()
            original = Exchange(reply_bytes(EXPIRED), after_send={2: (b"invalid history\n",)})
            replacement = Exchange(after_send={2: (reply_bytes(wire.BuildHistoryReply(7)),), 3: (reply_bytes(GRANTED),)})
            transport = self.use_transport(fixture, original, replacement)
            send = ScriptSocket.sendall
            wait_sent = False

            def acknowledge_replacement() -> None:
                nonlocal wait_sent
                if len(transport.requests) >= 3 and not wait_sent:
                    replacement.events.append(reply_bytes(EXPIRED))
                    wait_sent = True

            def check_request(peer: ScriptSocket, data: bytes) -> None:
                if (peer.exchange is replacement and not wait_sent
                        and isinstance(wire.parse_request(data), wire.BuildHistoryRequest)):
                    raise HistoryBeforeWait()
                send(peer, data)

            def refresh() -> wire.AdmitRequest:
                self.assertEqual(client.build_history_generation(), records.BuildHistoryGeneration(7))
                return request

            transport.on_selector_timeout = acknowledge_replacement
            with (mock.patch.object(ScriptSocket, "sendall", autospec=True, side_effect=check_request),
                  redirect_stderr(io.StringIO())):
                try:
                    result = client.admit(request, lambda _wait: None, refresh)
                except HistoryBeforeWait:
                    self.fail("history arrived before the replacement Wait")
            self.assertEqual(result, GRANTED)
            self.assertEqual(len(transport.sockets), 2)
            self.assertEqual([item for item in transport.requests if isinstance(item, wire.AdmitRequest)],
                             [request, request, request])

    def test_old_wait_cannot_extend_the_deadline_before_or_after_replacement_eof(self) -> None:
        for replacement_eof in (False, True):
            with self.subTest(replacement_eof=replacement_eof), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=2, reconnect_seconds=2, unreachable_limit_seconds=5,
                                  running_pass_seconds=3)
                request = fixture.request()
                old = Exchange()
                replacement = Exchange(b"") if replacement_eof else Exchange()
                transport = self.use_transport(fixture, old, replacement,
                    *(Exchange(connect_error=FileNotFoundError()) for _ in range(4)))
                waits: list[wire.Wait] = []

                def enqueue_old_wait() -> bool:
                    self.assertEqual(transport.requests[:2], [request, request])
                    self.assertEqual(transport.sockets[1].closed, replacement_eof)
                    old.events.append(reply_bytes(WAIT))
                    return False

                with (mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "leave"}),
                      mock.patch.object(client, "_leave_queue",
                          side_effect=lambda: enqueue_old_wait() if fixture.clock.monotonic() == 3 else False),
                      redirect_stderr(io.StringIO())):
                    result = client.admit(request, waits.append, lambda: request)
                self.assertEqual(result, wire.Refused("build queue unreachable for 5 s; step stopped; cargo token given back"))
                self.assertEqual(fixture.clock.monotonic(), 7)
                self.assertEqual(waits, [WAIT])
                self.doCleanups()

    def test_old_socket_grant_survives_a_blocked_replacement_connect_or_send(self) -> None:
        for blocked in ("connect", "send"):
            with self.subTest(blocked=blocked), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=2, reconnect_seconds=1, unreachable_limit_seconds=5)
                request = fixture.request()
                old, replacement = Exchange(), Exchange()
                replacement.writable = False
                transport = self.use_transport(fixture, old, replacement)
                connect = ScriptSocket.connect
                sendall = ScriptSocket.sendall

                def connect_at_stall(peer: ScriptSocket, path: str) -> None:
                    if peer.exchange is replacement and blocked == "connect":
                        if peer.nonblocking:
                            raise BlockingIOError(errno.EINPROGRESS, "connect pending")
                        raise AssertionError("the replacement used blocking connect")
                    connect(peer, path)

                def send_at_stall(peer: ScriptSocket, data: bytes) -> None:
                    if peer.exchange is replacement:
                        raise AssertionError("the replacement sent before it was writable")
                    sendall(peer, data)

                def grant_during_stall() -> None:
                    if len(transport.sockets) == 2:
                        old.events.append(reply_bytes(GRANTED))

                transport.on_selector_timeout = grant_during_stall
                with (mock.patch.object(ScriptSocket, "connect", autospec=True, side_effect=connect_at_stall),
                      mock.patch.object(ScriptSocket, "sendall", autospec=True, side_effect=send_at_stall),
                      redirect_stderr(io.StringIO())):
                    result = client.admit(request, lambda _wait: None, lambda: request)
                self.assertEqual(result, GRANTED)
                self.assertEqual(transport.attempts, [0, 2])
                self.assertEqual(transport.requests, [request])
                self.doCleanups()

    def test_hold_during_resend_keeps_the_deadline_and_releases_a_grant(self) -> None:
        for outcome in ("grant", "silent", "release"):
            with self.subTest(outcome=outcome), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=2, reconnect_seconds=1, unreachable_limit_seconds=5)
                request = fixture.request()
                final = replace(GRANTED, reservation="after hold")
                old = Exchange()
                tail = {"grant": [Exchange(reply_bytes(wire.Ok())), Exchange(reply_bytes(final))],
                        "silent": [Exchange()], "release": [Exchange(reply_bytes(final))]}
                transport = self.use_transport(fixture, old, *tail[outcome])
                hold_checks: list[float] = []
                hold_waits: list[float] = []
                hold_checks_after_release: list[tuple[float, int]] = []

                def admits() -> bool:
                    now = fixture.clock.monotonic()
                    hold_checks.append(now)
                    if now == 0:
                        self.assertEqual(transport.attempts, [])
                        return True
                    if outcome == "release":
                        return now >= 3
                    released = any(isinstance(item, wire.ReleaseRequest) for item in transport.requests)
                    if released:
                        hold_checks_after_release.append((now, len(transport.attempts)))
                    return released

                def wait_until_free() -> None:
                    hold_waits.append(fixture.clock.monotonic())
                    if len(hold_waits) > 1:
                        self.assertIsInstance(transport.requests[-1], wire.ReleaseRequest)

                def grant_during_hold() -> None:
                    if outcome == "grant" and fixture.clock.monotonic() == 3:
                        old.events.append(reply_bytes(GRANTED))

                transport.on_selector_timeout = grant_during_hold
                with redirect_stderr(io.StringIO()):
                    result = client.admit(request, lambda _wait: None, lambda: request,
                        _attempt=client.AdmissionHoldCheck(wait_until_free, admits))
                if outcome == "silent":
                    self.assertEqual(result, wire.Refused("build queue unreachable for 5 s; step stopped; cargo token given back"))
                    self.assertEqual(fixture.clock.monotonic(), 7)
                    self.assertEqual(transport.attempts, [0])
                    self.assertIn(3, hold_checks)
                else:
                    self.assertEqual(result, final)
                    self.assertEqual(fixture.clock.monotonic(), 3)
                    self.assertEqual(transport.attempts, [0, 3, 3] if outcome == "grant" else [0, 3])
                self.assertEqual(hold_waits, [])
                if outcome == "grant":
                    self.assertEqual(hold_checks_after_release[:1], [(3, 2)])
                self.assertEqual(hold_checks, {"grant": [0, 2, 3, 3, 3], "silent": [0, 2, 3, 4, 5, 6],
                                               "release": [0, 2, 3, 3]}[outcome])
                self.doCleanups()

    def test_post_grant_hold_checks_keep_the_active_deadline(self) -> None:
        for release_time, hold_ends in ((3, 5), (3, 9), (8, 9)):
            with self.subTest(release_time=release_time, hold_ends=hold_ends), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=2, reconnect_seconds=1, unreachable_limit_seconds=5)
                request = fixture.request()
                final = replace(GRANTED, reservation="after hold")
                original = Exchange()

                def release_grant(_item: wire.Request) -> wire.Reply:
                    fixture.clock.advance(release_time - fixture.clock.monotonic())
                    return wire.Ok()

                transport = self.use_transport(fixture, original, Exchange(answer=release_grant),
                                               Exchange(reply_bytes(final)))
                hold_waits: list[float] = []
                hold_checks: list[float] = []

                def wait_until_free() -> None:
                    now = fixture.clock.monotonic()
                    hold_waits.append(now)
                    if now > 0:
                        fixture.clock.advance(max(0, hold_ends - now))

                def admits() -> bool:
                    now = fixture.clock.monotonic()
                    hold_checks.append(now)
                    if now == 0:
                        self.assertEqual(transport.attempts, [])
                        return True
                    return now >= hold_ends

                def grant_during_hold() -> None:
                    if fixture.clock.monotonic() == 3:
                        original.events.append(reply_bytes(GRANTED))

                transport.on_selector_timeout = grant_during_hold
                with redirect_stderr(io.StringIO()):
                    result = client.admit(request, lambda _reply: None, lambda: request,
                                          _attempt=client.AdmissionHoldCheck(wait_until_free, admits))
                self.doCleanups()
                self.assertEqual(hold_waits, [])
                first, released, *later = transport.requests
                self.assertEqual(first, request)
                assert isinstance(released, wire.ReleaseRequest)
                self.assertEqual(released.reservation, GRANTED.reservation)
                if hold_ends < 7:
                    self.assertEqual(result, final)
                    self.assertEqual(fixture.clock.monotonic(), hold_ends)
                    second, = later
                    assert isinstance(second, wire.AdmitRequest)
                    self.assertNotEqual(second.episode, request.episode)
                    self.assertEqual(second.since, request.since)
                    self.assertEqual(transport.attempts, [0, 3, 5])
                    self.assertEqual(hold_checks, [0, 2, 3, 3, 4, 5, 5])
                else:
                    self.assertEqual(result, wire.Refused("build queue unreachable for 5 s; step stopped; cargo token given back"))
                    self.assertEqual(fixture.clock.monotonic(), max(release_time, 7))
                    self.assertEqual(later, [])
                    self.assertEqual(transport.attempts, [0, 3])
                    self.assertEqual(hold_checks, [0, 2, 3, 3, 4, 5, 6] if release_time == 3 else [0, 2, 3])

    def test_wait_reports_service_configuration_diagnostics(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            diagnostic = records.ConfigurationDiagnostic("warning: private service configuration fallback")
            wait = replace(WAIT, snapshot=replace(WAIT.snapshot, diagnostics=(diagnostic,)))
            _ = self.use_transport(fixture, Exchange(reply_bytes(wait), TimedChunk(0.01, reply_bytes(GRANTED))))
            waits: list[wire.Wait] = []
            with (mock.patch.object(records, "_warned_configuration_diagnostics", set[records.ConfigurationDiagnostic]()),
                  redirect_stderr(io.StringIO()) as output):
                result = client.admit(request, waits.append, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertEqual(waits, [wait])
            self.assertEqual(output.getvalue().splitlines().count(diagnostic.diagnostic), 1)

    def test_launch_zero_limit_refuses_first_hold_but_accepts_immediate_grant(self) -> None:
        for reply in (WAIT, wire.LaunchAdmitted(GIB, "new-source")):
            with self.subTest(reply=reply), AdmissionFixture() as fixture:
                request = fixture.launch()
                _ = self.use_transport(fixture, Exchange(TimedChunk(0.01, reply_bytes(reply))))
                with mock.patch.dict(os.environ, {"BUILDLOG_MEM_WAIT_LIMIT_S": "0"}):
                    result = client.launch(request, lambda _wait: None, lambda: request)
                self.assertEqual(result, wire.Refused("launch wait limit reached without admission")
                                 if isinstance(reply, wire.Wait) else reply)
                self.doCleanups()

    def test_mutations_resend_the_same_request_id_after_transport_closes(self) -> None:
        mutations: tuple[Callable[[], object], ...] = (
            lambda: client.release("stem", 137, records.read_memory_config()), lambda: client.width("stem", 3),
            lambda: client.priority_set("unit", "urgent", "director"))
        for mutation in mutations:
            with self.subTest(mutation=mutation), AdmissionFixture() as fixture:
                transport = self.use_transport(fixture, Exchange(b""), Exchange(reply_bytes(wire.Ok())))
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(mutation(), wire.Ok())
                self.assertEqual(len(transport.requests), 2)
                self.assertEqual(transport.requests[0], transport.requests[1])
                self.doCleanups()

    def test_cli_sends_one_run_identity_through_estimate_refresh(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            original = fixture.request()
            transport = self.use_transport(fixture, Exchange(reply_bytes(WAIT), reply_bytes(EXPIRED), after_send={2: (reply_bytes(GRANTED),)}))
            calls: list[int] = []

            def estimate(*_args: object, **_kwargs: object) -> step_estimator.StepEstimate:
                calls.append(len(calls))
                if len(calls) == 2:
                    os.environ["BUILDLOG_RUN"] = "999999:111111"
                return step_estimator.StepEstimate(original.profile, step_estimator.ReusableEstimate())

            with mock.patch.dict(os.environ, {"BUILDLOG_RUN": f"{run.pid}:{run.start_time}"}), \
                 mock.patch.object(step_estimator, "step_estimate", side_effect=estimate), \
                 mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                 redirect_stderr(io.StringIO()), mock.patch("sys.stdout", io.StringIO()) as output:
                status = shell_place.main(["admit", "--pid", str(original.owner.pid),
                                      "--since", str(original.since), "--wait-events", "--", "cargo", "nextest"])
            self.assertEqual(status, 0)
            self.assertEqual(len(calls), 2)
            first, second = transport.requests
            assert isinstance(first, wire.AdmitRequest)
            assert isinstance(second, wire.AdmitRequest)
            self.assertEqual((first.run, second.run), (run, run))
            self.assertEqual((first.owner, first.episode, first.since),
                             (second.owner, second.episode, second.since))
            self.assertEqual(output.getvalue().splitlines(), ["first", 'holder {"kind":"none"}', "done"])

    def test_missing_or_malformed_run_keeps_the_step_identity_and_admits(self) -> None:
        for value in (None, "", "missing", "42", "0:123", "-4:123", "abc:123", "42:", "42:abc", "42:1:2"):
            with self.subTest(value=value), AdmissionFixture() as fixture:
                original = fixture.request()
                transport = self.use_transport(fixture, Exchange(reply_bytes(GRANTED)))
                environment = dict(os.environ)
                if value is None:
                    _ = environment.pop("BUILDLOG_RUN", None)
                else:
                    environment["BUILDLOG_RUN"] = value
                with mock.patch.dict(os.environ, environment, clear=True), \
                     mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                         original.profile, step_estimator.ReusableEstimate())), \
                     mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                     redirect_stderr(io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
                    status = shell_place.main(["admit", "--pid", str(original.owner.pid),
                                          "--since", str(original.since), "--", "cargo", "nextest"])
                self.assertEqual(status, 0)
                request, = transport.requests
                assert isinstance(request, wire.AdmitRequest)
                self.assertIsInstance(request.run, wire.NoRun)
                self.assertEqual((request.owner, request.since), (original.owner, original.since))
            self.doCleanups()

    def test_mac_client_sends_no_run_even_with_an_inherited_linux_identity(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            original = fixture.request()
            transport = self.use_transport(fixture, Exchange(reply_bytes(GRANTED)))
            with mock.patch.object(sys, "platform", "darwin"), \
                 mock.patch.dict(os.environ, {"BUILDLOG_RUN": f"{run.pid}:{run.start_time}"}), \
                 mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                     original.profile, step_estimator.ReusableEstimate())), \
                 mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                 redirect_stderr(io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
                status = shell_place.main(["admit", "--pid", str(original.owner.pid), "--", "cargo", "nextest"])
            self.assertEqual(status, 0)
            request, = transport.requests
            assert isinstance(request, wire.AdmitRequest)
            self.assertIsInstance(request.run, wire.NoRun)

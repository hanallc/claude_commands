"""Test first-step admission and handoff."""

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
import os
from pathlib import Path
import subprocess
from typing import cast
import unittest
from unittest import mock

from scripts.build_hold import build_hold
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_records as records, execution_identity, queue_place
from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_wire, first_step, placement
from scripts.build_queue.admission_jobs import StepJob
from scripts.buildlog import treekey
from scripts.build_queue.test_placement import (
    SCALER_TARGET_NAME, PlacementFixture, adopt_assignment, assignment_for, consume_permission,
    local_candidate, mac_linux_candidate, placement_request, publish_candidate, register_scaler,
)
from scripts.copies import copies
from scripts.lib import build_folders, conf
from scripts.lib.build_slots import SlotQueuePosition
from scripts.lib.build_steps import CLAIM_FAILURE_SUFFIX, GATE_PRELAUNCH_REFUSED_STATUS, GATE_UNSETTLED_LAUNCH_STATUS, command_job
from scripts.mac_test import claim_state
from scripts.message.session_send_test_support import forbid_real_sender
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


GATE_COMMAND = ("python3", "scripts/production/gate.py", "--tests-only")
PROGRAM_GRANT = wire.Admitted("program", 1, "test", "", "", {})


def setUpModule() -> None:
    forbid_real_sender()


def forbid_admission_calls() -> AbstractContextManager[object]:
    """Fail at once when a test with mocked placement reaches an admission socket."""
    return mock.patch.object(client, "_call", side_effect=AssertionError("unmocked admission service call"))


def gate_source(fixture: PlacementFixture, repository: str, *, enabled: str = "on") -> Path:
    """Give the gate a private registry and repository origin."""
    _ = write_config_fixture(fixture.root / "build_targets.conf",
        "targets=mac-linux,natedev,mac,hana-aws\n"
        + f"enabled.mac-linux={enabled}\n")
    _ = write_config_fixture(fixture.root / "mac_linux.conf",
        "ssh.hops=fixture-mac fixture-guest\nguest.user=fixture\n")
    _ = write_config_fixture(fixture.root / "hana_aws.conf", "enabled=on\n")
    source = fixture.root / (".claude" if repository == "claude_commands" else repository)
    source.mkdir()
    for command in (("init", "-q"), ("remote", "add", "origin", f"https://example.invalid/{repository}.git"),
                    ("-c", "user.name=Gate Fixture", "-c", "user.email=gate@example.invalid",
                     "commit", "-q", "--allow-empty", "-m", "fixture")):
        _ = subprocess.run(("git", "-C", str(source), *command), capture_output=True,
                           check=True, timeout=TEST_HANG_GUARD_SECS)
    return source


class FirstStepTests(unittest.TestCase):
    def test_late_settlement_reply_uses_placement_control(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            destination = register_scaler(fixture).destination
            assignment = placement.PlacementAssignment(request.episode, "a", destination,
                request.constraints.source_key, request.owner, request.constraints.session, request.since)

            def late_reply(_connection: object, _pending: bytearray, deadline: float) -> bytes:
                fixture.clock.advance(1)
                if fixture.clock.monotonic() >= deadline:
                    raise TimeoutError("the short query expired")
                return wire.encode_reply(wire.Ok()).encode()

            fixture.configure(query_timeout_seconds=0.5)
            with (mock.patch.object(client, "CLOCK", fixture.clock),
                  mock.patch.object(client, "_connect", return_value=nullcontext(mock.Mock())),
                  mock.patch.object(client, "_line", side_effect=late_reply)):
                client.request_settlement(wire.SettlementRequest(assignment))

    def test_scaler_replacement_ignores_the_app_launch_wait_limit(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            cloud = register_scaler(fixture)

            def grant(request: wire.PlacementRequest) -> wire.Assignment:
                return wire.Assignment(placement.PlacementAssignment(request.episode, str(place.call_count),
                    cloud.destination, request.constraints.source_key, request.owner,
                    request.constraints.session, request.since))

            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": cloud.destination.target,
                                               "BUILDLOG_MEM_WAIT_LIMIT_S": "0"}),
                  mock.patch.object(records, "process_start_time", return_value="123"),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(first_step, "_placement_grant", side_effect=grant) as place,
                  mock.patch.object(client, "cancel_placement", return_value=wire.Ok()),
                  forbid_admission_calls()):
                with first_step.admitted_run(GATE_COMMAND, source) as admitted:
                    assignment = client.read_assignment(admitted.assignment_file).assignment
                    refusal = wire.ClaimFailure("capacity", assignment, "no capacity")
                    report = admitted.assignment_file.with_name(admitted.assignment_file.name + CLAIM_FAILURE_SUFFIX)
                    _ = report.write_text(wire.encode_request(refusal))
                    reply = wire.DispatchStatus(assignment, placement.CurrentAssignment(),
                        placement.PermissionAvailable(), retry=placement.RetryAt(0))
                    run = mock.Mock(side_effect=[subprocess.CompletedProcess(GATE_COMMAND, GATE_PRELAUNCH_REFUSED_STATUS),
                                                 subprocess.CompletedProcess(GATE_COMMAND, 0)])
                    with mock.patch.object(client, "claim_failure", return_value=reply) as claim:
                        result = admitted.run_gate(run)
                    claim.assert_called_once_with(refusal)
                    self.assertEqual(place.call_count, 2)
                    self.assertEqual(place.call_args_list[0], place.call_args_list[1])
                    self.assertEqual(run.call_count, 2)
                    self.assertEqual(result.returncode, 0)

    def test_copies_run_hana_gate_keeps_unassigned_route(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "hana")
            script = source / "gate.py"
            _ = script.write_text("import os\nassert 'BUILDLOG_PLACEMENT_FILE' not in os.environ\n")
            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}),
                  mock.patch.object(copies, "caller_owner", return_value="fixture"),
                  mock.patch.object(copies, "requested_source", return_value=nullcontext(source)),
                  mock.patch.object(copies, "commit_at", return_value="fixture-tip"),
                  mock.patch.object(copies, "standing", return_value=nullcontext(source)),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(queue_place, "request_place", return_value=PROGRAM_GRANT) as program,
                  mock.patch.object(queue_place, "release_place", return_value=wire.Ok()),
                  mock.patch.object(first_step, "_placement_grant", side_effect=AssertionError("Hana placement")),
                  forbid_admission_calls()):
                self.assertEqual(copies.main(["run", "--repo", str(source), "--commit", "HEAD", "--", "python3", str(script),
                                              "--tests-only"]), 0)
                program.assert_called_once()

    def test_gate_modes_submit_one_default_request(self) -> None:
        self.check_gate_mode_candidates(register_aws=True)

    def test_gate_without_scaler_registration_requests_slice_one_targets(self) -> None:
        self.check_gate_mode_candidates(register_aws=False)

    def test_standalone_module_and_nightly_gates_only_place_on_natedev(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            cloud = register_scaler(fixture)
            local = local_candidate(fixture)

            def grant(request: wire.PlacementRequest) -> wire.LocalPlacementGranted:
                assignment = placement.PlacementAssignment(request.episode, "attempt", local.destination,
                    request.constraints.source_key, request.owner, request.constraints.session, request.since)
                return wire.LocalPlacementGranted(assignment, PROGRAM_GRANT)

            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(first_step, "_placement_grant", side_effect=grant) as submit,
                  mock.patch.object(client, "cancel_placement", return_value=wire.Ok()),
                  forbid_admission_calls()):
                for arguments in (("--tests-only", "--module=lib/test_gate.py"),
                                  ("--nightly", "--merge-branch", "main", "--unit", "fixture")):
                    with self.subTest(arguments=arguments):
                        command = (*GATE_COMMAND[:2], *arguments)
                        submit.reset_mock()
                        with first_step.admitted_run(command, source) as admitted:
                            grant_for_gate = admitted.grant
                            assert isinstance(grant_for_gate, first_step.OwnedGatePlacement)
                            self.assertEqual(grant_for_gate.grant.assignment.destination, local.destination)
                            self.assertEqual(grant_for_gate.request.constraints.candidates, (local,))
                        submit.assert_called_once()
                        names = (cloud.destination.target, "mac-linux", "natedev")
                        with first_step.admitted_run(command, source, targets=names) as admitted:
                            mixed_grant = admitted.grant
                            assert isinstance(mixed_grant, first_step.OwnedGatePlacement)
                            candidates = mixed_grant.request.constraints.candidates
                            self.assertEqual(tuple(c.destination.target for c in candidates), names)
                            self.assertEqual(tuple(c for c in candidates
                                if isinstance(c.eligibility, placement.AllowedHost)), (local,))
                        with self.assertRaisesRegex(first_step.FirstStepRefused, "runs only on natedev"):
                            with first_step.admitted_run(command, source, targets=names[:-1]):
                                self.fail("a remote-only gate request was accepted")
                        self.assertEqual(submit.call_count, 2)

    def check_gate_mode_candidates(self, *, register_aws: bool) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            cloud = (register_scaler(fixture),) if register_aws else ()
            expected = (*cloud, mac_linux_candidate(), local_candidate(fixture))
            requests: list[wire.PlacementRequest] = []
            wait_for_grant = cast(Callable[[wire.PlacementRequest], wire.Assignment | wire.LocalPlacementGranted],
                                  getattr(first_step, "_placement_grant"))

            def grant(request: wire.PlacementRequest) -> wire.Assignment:
                requests.append(request)
                return wire.Assignment(placement.PlacementAssignment(request.episode, str(len(requests)),
                    request.constraints.candidates[0].destination, request.constraints.source_key, request.owner,
                    request.constraints.session, request.since))

            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}),
                  mock.patch.object(records, "process_start_time", return_value="123"),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(first_step, "_placement_grant", side_effect=grant),
                  mock.patch.object(queue_place, "request_place", side_effect=AssertionError("plain memory admission")),
                  mock.patch.object(client, "cancel_placement", return_value=wire.Ok()),
                  forbid_admission_calls()):
                for options in ((), ("--affected",), ("--tests-only",)):
                    command = (*GATE_COMMAND[:2], *options)
                    with first_step.admitted_run(command, source) as admitted:
                        self.assertIsInstance(admitted.grant, first_step.GatePlacement)
                    self.assertEqual(requests[-1].constraints.candidates, expected)
                self.assertEqual(len(requests), 3)
                script = source / "gate.py"
                _ = script.write_text("raise SystemExit(0)\n")
                self.assertEqual(copies.main(["gate-step", "--repo", str(source), "--",
                                             "python3", str(script), "--tests-only"]), 0)
                self.assertEqual(len(requests), 4)
                self.assertEqual(requests[-1].constraints.candidates, expected)
                with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "natedev"}):
                    with first_step.admitted_run(GATE_COMMAND, source) as admitted:
                        self.assertIsInstance(admitted.grant, first_step.GatePlacement)
                self.assertEqual(len(requests), 5)
                self.assertEqual(requests[-1].constraints.candidates, (local_candidate(fixture),))
                with first_step.admitted_run(GATE_COMMAND, source) as outer:
                    outer.handoff(source)
                    saved = outer.grant
                    assert isinstance(saved, first_step.OwnedGatePlacement)
                    sidecar = str(fixture.root / "placement.cgroup")
                    self.assertEqual(os.environ["BUILDLOG_PLACEMENT_SIDECAR"], sidecar)
                    assigned = saved.grant.assignment
                    status = admission_wire.KnownPlacementStatus(assigned, placement.CurrentAssignment(),
                        placement.NoProviderClaim())
                    owner = build_hold.ProcessIdentity(assigned.owner.pid, assigned.owner.start_time)
                    with (mock.patch.object(build_hold, "caller_processes", return_value={owner}),
                          mock.patch.object(client, "placement_status",
                                            return_value=admission_wire.PlacementStatusReply(status))):
                        with first_step.admitted_run(GATE_COMMAND, source) as attached:
                            self.assertEqual(attached.grant, first_step.InheritedGatePlacement(saved.grant))
                            self.assertEqual(attached.assignment_file, outer.assignment_file)
                            attached.handoff(source)
                            self.assertEqual(os.environ["BUILDLOG_PLACEMENT_SIDECAR"], sidecar)
                        different_source = fixture.root / "different-checkout"
                        with (mock.patch.object(treekey, "tree_key", return_value="different-key"),
                              self.assertRaisesRegex(first_step.FirstStepRefused, "source")):
                            with first_step.admitted_run(GATE_COMMAND, different_source):
                                self.fail("the wrapper assignment accepted a different source")
                self.assertEqual(len(requests), 6)
                if register_aws:
                    registry = fixture.root / "build_targets.conf"
                    cloud_settings = fixture.root / "cloud_gate.conf"
                    for switch_file, key in ((registry, f"enabled.{SCALER_TARGET_NAME}"), (cloud_settings, "enabled")):
                        _ = write_config_fixture(switch_file, f"{key}=off\n")
                        self.assertEqual(first_step.gate_targets(GATE_COMMAND, source), ("mac-linux", "natedev"))
                        _ = write_config_fixture(switch_file, f"{key}=on\n")
                    for exit_code in (GATE_PRELAUNCH_REFUSED_STATUS, GATE_UNSETTLED_LAUNCH_STATUS):
                        with self.subTest(exit_code=exit_code), first_step.admitted_run(GATE_COMMAND, source) as admitted:
                            admitted.handoff(source)
                            original = client.read_assignment(admitted.assignment_file).assignment
                            self.assertEqual(original.destination.provider, "scaler")
                            refusal = wire.ClaimFailure("decline", original, "no capacity")
                            report = admitted.assignment_file.with_name(admitted.assignment_file.name + CLAIM_FAILURE_SUFFIX)
                            _ = report.write_text(admission_wire.encode_request(refusal))
                            settled = wire.DispatchStatus(original, placement.ClosedAssignment(refusal.reason),
                                placement.PermissionAvailable(), retry=placement.RetryAt(0))
                            return_codes = iter((exit_code, 0))

                            def run_attempt() -> subprocess.CompletedProcess[str]:
                                self.assertEqual(os.environ.pop("BUILDLOG_PLACEMENT_SIDECAR"), sidecar)
                                return subprocess.CompletedProcess(GATE_COMMAND, next(return_codes))

                            run = mock.Mock(side_effect=run_attempt)
                            with (mock.patch.object(client, "claim_failure", return_value=settled) as decline,
                                  mock.patch.object(client, "request_settlement") as settle):
                                self.assertEqual(admitted.run_gate(run).returncode, 0)
                            self.assertEqual(requests[-2], requests[-1])
                            local = requests[-1].local
                            assert isinstance(local, wire.LocalAdmissionContext)
                            self.assertEqual(local.sidecar, sidecar)
                            self.assertEqual(run.call_count, 2)
                            self.assertEqual(decline.call_count, int(exit_code == GATE_PRELAUNCH_REFUSED_STATUS))
                            if exit_code == GATE_UNSETTLED_LAUNCH_STATUS:
                                settle.assert_called_once()
                                sent = cast(object, settle.call_args.args[0])
                                assert isinstance(sent, wire.SettlementRequest)
                                self.assertEqual(sent.assignment, original)
                            else:
                                settle.assert_not_called()
                            if exit_code == GATE_UNSETTLED_LAUNCH_STATUS:
                                could_not_settle = wire.AdmissionUnavailable("the queue is unavailable")
                                run = mock.Mock(return_value=subprocess.CompletedProcess(GATE_COMMAND, exit_code))
                                with mock.patch.object(client, "request_settlement", side_effect=could_not_settle):
                                    blocked = admitted.run_gate(run)
                                self.assertEqual(blocked.returncode, GATE_UNSETTLED_LAUNCH_STATUS)
                                self.assertIn("launch settlement", blocked.stderr)
                                run.assert_called_once()

                    with first_step.admitted_run(GATE_COMMAND, source) as admitted:
                        admitted.handoff(source)
                        original = client.read_assignment(admitted.assignment_file).assignment
                        refusal = wire.ClaimFailure("last-decline", original, "no capacity")
                        report = admitted.assignment_file.with_name(admitted.assignment_file.name + CLAIM_FAILURE_SUFFIX)
                        _ = report.write_text(admission_wire.encode_request(refusal))
                        settled = wire.DispatchStatus(original, placement.ClosedAssignment(refusal.reason),
                            placement.PermissionAvailable(), retry=placement.RetryAt(0))
                        exhausted = wire.DispatchStatus(placement.UnknownAssignment(original.episode, ""),
                            placement.ClosedAssignment(refusal.reason), placement.NoLaunchPermission(refusal.reason),
                            refusal.reason, placement.DoNotRetry(refusal.reason))
                        run = mock.Mock(return_value=subprocess.CompletedProcess(GATE_COMMAND, GATE_PRELAUNCH_REFUSED_STATUS))
                        with (mock.patch.object(client, "claim_failure", return_value=settled),
                              mock.patch.object(first_step, "_placement_grant", side_effect=wait_for_grant),
                              mock.patch.object(queue_place, "wait_for_placement", return_value=exhausted)):
                            result = admitted.run_gate(run)
                        self.assertEqual(result.returncode, GATE_PRELAUNCH_REFUSED_STATUS)
                        self.assertEqual(result.stderr, "Gate placement blocked\n")
                        run.assert_called_once()

    def test_explicit_gate_targets_override_the_claude_default(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "mac-linux"}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source, requested="mac"), ("mac",))
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ("mac-linux",))

    def test_hana_gate_has_no_default_targets(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "hana")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ())

    def test_disabled_targets_leave_the_claude_gate_default(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands", enabled="off")
            _ = write_config_fixture(fixture.root / "build_targets.conf", "enabled.natedev=off\n")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                with self.assertRaisesRegex(first_step.FirstStepRefused, "No enabled build target"):
                    _ = first_step.gate_targets(GATE_COMMAND, source=source)

    def test_disabling_only_mac_linux_keeps_natedev_in_the_claude_gate_default(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands", enabled="off")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ("natedev",))

    def test_default_gate_omits_an_unsupported_linux_target(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            _ = write_config_fixture(fixture.root / "build_targets.conf",
                "targets=linux-extra,natedev\nkind.linux-extra=ssh\nplatform.linux-extra=linux\n"
                + "settings.linux-extra=linux-extra.conf\n")
            _ = (fixture.root / "linux-extra.conf").write_text("host=fixture-linux\nkinds=check,test\n")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ("natedev",))

    def test_default_gate_omits_mac_linux_when_its_enable_key_is_missing(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            registry = fixture.root / "build_targets.conf"
            _ = registry.write_text(registry.read_text().replace("enabled.mac-linux=on\n", ""))
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ("natedev",))

    def test_local_gate_keeps_its_reservation_and_assignment_through_the_handoff(self) -> None:
        position = SlotQueuePosition("episode", 3)
        assignment = placement.PlacementAssignment(position.episode, "attempt",
            placement.PlacementDestination("local", "natedev", "fixture-host", claim_state.WHOLE_MAC),
            execution_identity.SourceKeyKnown("source"), placement.SubmittingOwner("fixture-host", 1, "2"),
            NoOwningSession(), position.since)
        profile = records.ProgramProfile("gate", "/fixture", "/fixture",
            records.HistoryPeakSizing(1, records.FirstProgramPeak(1), 0.0))
        request = wire.PlacementRequest(position.episode, assignment.owner, position.since, profile,
            placement.PlacementConstraints(assignment.source_key, ("gate.py",), "/fixture", "", assignment.session,
                (placement.PlacementCandidate(assignment.destination),)))
        grant = first_step.OwnedGatePlacement(wire.LocalPlacementGranted(assignment, PROGRAM_GRANT), request)
        admitted = first_step.FirstStepAdmission(position, grant, Path("/fixture/assignment.json"))

        with (mock.patch.dict(os.environ),
              mock.patch.object(queue_place, "release_place", return_value=wire.Ok()) as release,
              mock.patch.object(client, "cancel_placement", return_value=wire.Ok()) as cancel):
            admitted.handoff(Path("/fixture"))
            # The gate ends the reservation after it confirms the assignment.
            release.assert_not_called()
            cancel.assert_not_called()
            self.assertEqual(os.environ["BUILDLOG_PLACEMENT_FILE"], str(admitted.assignment_file))
            self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], assignment.episode)
            self.assertEqual(admitted.grant, grant)
            admitted.close()
            cancel.assert_called_once()
            self.assertIsInstance(admitted.grant, first_step.HandedOff)

    def test_named_gate_places_one_request_and_exports_the_assignment(self) -> None:
        self.check_gate_placement((*GATE_COMMAND[:2], "--affected"),
                                  ("test-affected", "test-affected", "test affected"), use_default=False)

    def test_default_claude_gate_places_one_request_and_exports_the_assignment(self) -> None:
        self.check_gate_placement(GATE_COMMAND, ("gate", "test-all", "test all"), use_default=True)

    def check_gate_placement(self, command: tuple[str, ...], expected_identity: tuple[str, str, str], *,
                             use_default: bool) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            configured = conf.read("build_targets").text("targets").split(",")
            expected = tuple(name for name in configured if name not in ("mac", "hana-aws"))
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                targets = first_step.gate_targets(command, source=source) if use_default else expected[::-1]
                if use_default:
                    self.assertEqual(targets, expected)
                    for arguments in (("--tests-only", "--module=fixture/test_gate.py"),
                                      ("--nightly", "--merge-branch", "main", "--unit", "fixture"),
                                      ("--promote-only", "--merge-branch", "main")):
                        local_only = (*GATE_COMMAND[:2], *arguments)
                        self.assertEqual(first_step.gate_targets(local_only, source=source), ())
                        self.assertEqual(first_step.gate_targets(local_only, source=source, requested="natedev"),
                                         ("natedev",))
                    _ = write_config_fixture(fixture.root / "build_targets.conf", "targets=natedev,mac,hana-aws\n")
                    self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ("natedev",))
                    _ = write_config_fixture(fixture.root / "build_targets.conf", "targets=" + ",".join(configured) + "\n")
            remote = mac_linux_candidate()
            local = local_candidate(fixture)
            requests: list[wire.PlacementRequest] = []

            def grant(request: wire.PlacementRequest) -> wire.Assignment:
                requests.append(request)
                return wire.Assignment(placement.PlacementAssignment(request.episode, "attempt", remote.destination,
                    request.constraints.source_key, request.owner, request.constraints.session, request.since))

            with (mock.patch.dict(os.environ,
                      {"BUILDLOG_FIRST_STEP_ASSIGNMENT": "outer-file", "BUILDLOG_PLACEMENT_EPISODE": "outer-episode",
                       "BUILDLOG_PLACEMENT_SIDECAR": "outer-sidecar"}),
                  mock.patch.object(records, "process_start_time", return_value="123"),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(client, "acquire_placement", side_effect=grant),
                  mock.patch.object(queue_place, "request_place", return_value=PROGRAM_GRANT) as program,
                  mock.patch.object(queue_place, "release_place", return_value=wire.Ok()),
                  mock.patch.object(client, "cancel_placement", return_value=wire.Ok()) as cancel,
                  forbid_admission_calls()):
                _ = os.environ.pop("BUILDLOG_PLACEMENT_FILE", None)
                with first_step.admitted_run(command, source, targets=targets) as admitted:
                    self.assertEqual(len(requests), 1)
                    request = requests[0]
                    self.assertEqual(request.constraints.candidates, (remote, local))
                    self.assertEqual(request.constraints.command, command)
                    self.assertEqual(request.run, wire.RunIdentity(os.getpid(), "123"))
                    job = request.identity.job
                    self.assertEqual(job, command_job(command))
                    assert isinstance(job, StepJob)
                    self.assertEqual((request.identity.step, job.step, request.identity.what), expected_identity)
                    self.assertIsInstance(request.profile, records.ProgramProfile)
                    assert isinstance(request.local, wire.LocalAdmissionContext)
                    self.assertEqual(request.local.argv, (expected_identity[0],))
                    self.assertEqual(request.local.sidecar, str(fixture.root / "placement.cgroup"))
                    self.assertNotIn("BUILDLOG_PLACEMENT_SIDECAR", os.environ)

                    saved = client.read_assignment(admitted.assignment_file).assignment
                    self.assertEqual(saved.owner, request.owner)
                    self.assertEqual(saved.destination, remote.destination)
                    admitted.handoff(fixture.root)
                    self.assertEqual(os.environ["BUILDLOG_PLACEMENT_FILE"], str(admitted.assignment_file))
                    self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], saved.episode)
                    self.assertEqual(os.environ["BUILDLOG_PLACEMENT_SIDECAR"], request.local.sidecar)
                    self.assertNotIn("BUILDLOG_FIRST_STEP_ASSIGNMENT", os.environ)
                    cancel.assert_not_called()
                self.assertNotIn("BUILDLOG_PLACEMENT_FILE", os.environ)
                self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], "outer-episode")
                self.assertEqual(os.environ["BUILDLOG_PLACEMENT_SIDECAR"], "outer-sidecar")
                self.assertEqual(os.environ["BUILDLOG_FIRST_STEP_ASSIGNMENT"], "outer-file")
                cancel.assert_called_once()
                program.assert_not_called()

    def test_unnamed_gate_uses_program_memory_admission(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "hana")
            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}),
                  mock.patch.object(records, "process_start_time", return_value="123"),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(first_step, "_placement_grant") as place,
                  mock.patch.object(queue_place, "request_place", return_value=PROGRAM_GRANT) as program,
                  mock.patch.object(queue_place, "release_place", return_value=wire.Ok()),
                  forbid_admission_calls()):
                with first_step.admitted_run(GATE_COMMAND, source) as admitted:
                    refresh = cast(object, program.call_args.args[0])
                    assert isinstance(refresh, queue_place.RefreshedRequest)
                    request = refresh.refresh()
                    profile = request.profile
                    assert isinstance(profile, records.ProgramProfile)
                    self.assertEqual(profile.program, "gate")
                    self.assertGreater(profile.sizing.first_peak_bytes, 0)
                    self.assertEqual(request.argv, ("gate",))
                    self.assertEqual(request.identity.what, "test all")
                    admitted.handoff(source)
                    self.assertNotIn("BUILDLOG_PLACEMENT_FILE", os.environ)
                    self.assertNotIn("BUILDLOG_PLACEMENT_SIDECAR", os.environ)
                    self.assertFalse(admitted.assignment_file.exists())
                place.assert_not_called()

    def test_close_after_verify_cancels_the_completed_remote_placement(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            claim = adopt_assignment(fixture, request, assignment)
            grant = wire.Assignment(assignment)
            path = fixture.root / "assignment.json"
            client.write_assignment(path, grant)
            admitted = first_step.FirstStepAdmission(SlotQueuePosition(request.episode, request.since), grant, path)

            admitted.handoff(fixture.root)
            self.assertEqual(os.environ["BUILDLOG_FIRST_STEP_ASSIGNMENT"], str(path))
            self.assertIsInstance(fixture.connect(consume_permission("launch", assignment, claim)).replies()[-1],
                                  wire.FirstAuthorization)
            _ = publish_candidate(fixture, request)
            completed, = fixture.connect(wire.ExecutionReport("done", assignment, claim,
                execution_identity.ExecutionNotStarted(), placement.AttemptCompleted(0), placement.DispatchPathSettled())).replies()
            assert isinstance(completed, wire.DispatchStatus)
            self.assertEqual(completed.state, placement.ClosedAssignment("completed"))

            with fixture.serving():
                self.assertEqual(client.cancel_placement(wire.CancelPlacement(
                    "verify-exit", request.episode, request.owner, "verify ended")), wire.Ok())
                with fixture.loop_turn():
                    self.assertNotIn(request.episode, fixture.server.placement_requests)
                admitted.close()
                with self.assertRaises(wire.AdmissionUnavailable) as refusal:
                    _ = client.cancel_placement(wire.CancelPlacement(
                        "after-close", request.episode, request.owner, "verify ended"))
                self.assertEqual(refusal.exception.reason, wire.PLACEMENT_REQUEST_MISSING_REASON)
            self.assertIsInstance(admitted.grant, first_step.HandedOff)

    def test_close_propagates_a_coordinator_mismatch(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            other_owner = replace(assignment.owner, start_time=assignment.owner.start_time + "0")
            grant = wire.Assignment(replace(assignment, owner=other_owner))
            admitted = first_step.FirstStepAdmission(SlotQueuePosition(request.episode, request.since),
                                                     grant, fixture.root / "assignment.json")

            with fixture.serving(), self.assertRaisesRegex(wire.AdmissionUnavailable,
                                                          "^placement coordinator does not match$"):
                admitted.close()
            self.assertEqual(admitted.grant, grant)

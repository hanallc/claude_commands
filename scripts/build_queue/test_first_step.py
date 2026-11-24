"""Test first-step admission and handoff."""

from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_records as records, execution_identity, queue_place
from scripts.build_queue import admission_client as client, admission_protocol as wire, first_step, placement
from scripts.build_queue.admission_jobs import StepJob
from scripts.build_queue.test_placement import (
    PlacementFixture, adopt_assignment, assignment_for, consume_permission, local_candidate, mac_linux_candidate,
    placement_request, publish_candidate,
)
from scripts.lib import build_folders, conf
from scripts.lib.build_slots import SlotQueuePosition
from scripts.lib.build_steps import command_job
from scripts.mac_test import claim_state
from scripts.message.session_send_test_support import forbid_real_sender
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


GATE_COMMAND = ("python3", "scripts/production/gate.py", "--tests-only")
MODULE_GATE_COMMAND = (*GATE_COMMAND[:2], "--affected", "--module", "lib/test_infrastructure.py")
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
    _ = write_config_fixture(fixture.root / "cloud_gate.conf", "enabled=off\n")
    source = fixture.root / (".claude" if repository == "claude_commands" else repository)
    source.mkdir()
    for command in (("init", "-q"), ("remote", "add", "origin", f"https://example.invalid/{repository}.git")):
        _ = subprocess.run(("git", "-C", str(source), *command), capture_output=True,
                           check=True, timeout=TEST_HANG_GUARD_SECS)
    return source


def register_scaler(root: Path, *, enabled: str = "on") -> None:
    _ = write_config_fixture(root / "build_targets.conf",
        "targets=mac-linux,natedev,mac,hana-aws,claude-aws\n"
        + "kind.claude-aws=scaler\nsettings.claude-aws=cloud_gate.conf\n"
        + f"enabled.claude-aws={enabled}\n")


class FirstStepTests(unittest.TestCase):
    def test_explicit_gate_targets_override_the_claude_default(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "mac-linux"}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source, requested="mac"), ("mac",))
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ("mac-linux",))

    def test_direct_nightly_default_excludes_aws_when_cloud_gates_are_enabled(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            command = (*GATE_COMMAND[:2], "--nightly")
            register_scaler(fixture.root)
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                self.assertEqual(first_step.gate_targets(command, source), ("mac-linux", "natedev"))

    def test_only_the_canonical_gate_can_request_aws(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source), ("mac-linux", "natedev"))
                canonical = source / "scripts/production/gate.py"
                # Aliases never receive a scaler assignment.
                canonical.parent.mkdir(parents=True)
                canonical.touch()
                (source / "gate.py").symlink_to(canonical)
                (source / "gate-alias.py").symlink_to(canonical)
                register_scaler(fixture.root, enabled="off")
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source), ("mac-linux", "natedev"))
                register_scaler(fixture.root)
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source), ("mac-linux", "natedev", "claude-aws"))
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source, requested="claude-aws"), ("claude-aws",))
                self.assertEqual(first_step.gate_targets(("python3", "gate-alias.py"), source,
                                                         requested="claude-aws,natedev"), ())
                for command in (("python3", "scripts/production/merge_gate.py", "--tests-only"),
                                ("python3", "scripts/production/targeted_test.py"),
                                ("python3", "-m", "scripts.production.gate", "--tests-only"),
                                ("python3", "gate.py", "--tests-only"),
                                (*GATE_COMMAND[:2], "--nightly"), MODULE_GATE_COMMAND):
                    with self.subTest(command=command):
                        self.assertNotIn("claude-aws", first_step.gate_targets(command, source))
                        self.assertEqual(first_step.gate_targets(command, source, requested="claude-aws,natedev"), ("natedev",))
                        with self.assertRaisesRegex(first_step.FirstStepRefused, "AWS requires scripts/production/gate.py"):
                            _ = first_step.gate_targets(command, source, requested="claude-aws")


    def test_first_step_submits_the_registered_scaler(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            register_scaler(fixture.root)
            owner = fixture.fixture_owner(os.getpid(), "123")

            def grant(request: wire.PlacementRequest, _reason: str) -> wire.Assignment:
                candidate, = request.constraints.candidates
                self.assertEqual(candidate.destination.target, "claude-aws")
                self.assertEqual(candidate.destination.provider, "scaler")
                self.assertEqual(candidate.settings, placement.ExplicitProviderSettings(fixture.root / "cloud_gate.conf"))
                self.assertEqual(candidate.eligibility, placement.AllowedHost())
                raise first_step.FirstStepRefused("The fixture checked the gate request")

            with (mock.patch.object(records, "process_start_time", return_value=owner.start_time),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(queue_place, "wait_for_placement", side_effect=grant) as place,
                  self.assertRaisesRegex(first_step.FirstStepRefused, "The fixture checked the gate request")):
                with first_step.admitted_run(GATE_COMMAND, source, targets=("claude-aws",)):
                    self.fail("The gate received a placement")
            place.assert_called_once()

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

    def test_named_module_gate_keeps_the_guest_excluded_from_placement(self) -> None:
        self.check_named_gate_candidates(MODULE_GATE_COMMAND,
            placement.FixedExclusion("--module runs only on natedev"), "natedev")

    def test_named_module_gate_refuses_targets_without_natedev(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            with (mock.patch.object(records, "process_start_time", return_value="123"),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(queue_place, "wait_for_placement",
                      side_effect=first_step.FirstStepRefused("The gate reached the queue")) as place):
                with self.assertRaisesRegex(first_step.FirstStepRefused, r"^gate\.py --module runs only on natedev$"):
                    with first_step.admitted_run(MODULE_GATE_COMMAND, source, targets=("mac-linux",)):
                        self.fail("The module gate received a placement")
                place.assert_not_called()

    def test_named_plain_gate_keeps_both_targets_eligible(self) -> None:
        self.check_named_gate_candidates(GATE_COMMAND, placement.AllowedHost(), "mac-linux")

    def check_named_gate_candidates(self, command: tuple[str, ...],
                                    guest_eligibility: placement.AllowedHost | placement.FixedExclusion,
                                    target: str) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            owner = fixture.fixture_owner(os.getpid(), "123")

            def grant(request: wire.PlacementRequest) -> wire.LocalPlacementGranted | wire.Assignment:
                _ = publish_candidate(fixture, request)
                reply, = fixture.connect(request).replies()
                assert isinstance(reply, (wire.Assignment, wire.LocalPlacementGranted)), reply
                return reply

            with (mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(fixture.root / "target-state")}),
                  mock.patch.object(records, "process_start_time", return_value=owner.start_time),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(client, "acquire_placement", side_effect=grant),
                  mock.patch.object(client, "cancel_placement", return_value=wire.Ok())):
                with first_step.admitted_run(command, source, targets=("natedev", "mac-linux")) as admitted:
                    placed = admitted.grant
                    assert isinstance(placed, first_step.GatePlacement)
                    self.assertEqual(placed.grant.assignment.destination.target, target)
                    guest = replace(mac_linux_candidate(), eligibility=guest_eligibility)
                    self.assertEqual(placed.request.constraints.candidates, (guest, local_candidate(fixture)))

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
        grant = first_step.GatePlacement(wire.LocalPlacementGranted(assignment, PROGRAM_GRANT), request)
        admitted = first_step.FirstStepAdmission(position, grant, Path("/fixture/assignment.json"))

        with (mock.patch.dict(os.environ),
              mock.patch.object(queue_place, "release_place", return_value=wire.Ok()) as release,
              mock.patch.object(client, "cancel_placement", return_value=wire.Ok()) as cancel):
            admitted.handoff(Path("/fixture"))
            # The gate keeps the reservation through the suites.
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
                    nightly = (*GATE_COMMAND[:2], "--nightly", "--merge-branch", "main", "--unit", "fixture")
                    self.assertEqual(first_step.gate_targets(nightly, source=source), expected)
                    for arguments in (("--tests-only", "--module=fixture/test_gate.py"),
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
                      {"BUILDLOG_PLACEMENT_FILE": "outer-file", "BUILDLOG_PLACEMENT_EPISODE": "outer-episode",
                       "BUILDLOG_PLACEMENT_SIDECAR": "outer-sidecar"}),
                  mock.patch.object(records, "process_start_time", return_value="123"),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(client, "acquire_placement", side_effect=grant),
                  mock.patch.object(queue_place, "request_place", return_value=PROGRAM_GRANT) as program,
                  mock.patch.object(queue_place, "release_place", return_value=wire.Ok()),
                  mock.patch.object(client, "cancel_placement", return_value=wire.Ok()) as cancel,
                  forbid_admission_calls()):
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

                    saved = client.read_assignment(admitted.assignment_file).assignment
                    self.assertEqual(saved.owner, request.owner)
                    self.assertEqual(saved.destination, remote.destination)
                    admitted.handoff(fixture.root)
                    self.assertEqual(os.environ["BUILDLOG_PLACEMENT_FILE"], str(admitted.assignment_file))
                    self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], saved.episode)
                    self.assertEqual(os.environ["BUILDLOG_PLACEMENT_SIDECAR"], request.local.sidecar)
                    self.assertNotIn("BUILDLOG_FIRST_STEP_ASSIGNMENT", os.environ)
                    cancel.assert_not_called()
                self.assertEqual(os.environ["BUILDLOG_PLACEMENT_FILE"], "outer-file")
                self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], "outer-episode")
                self.assertEqual(os.environ["BUILDLOG_PLACEMENT_SIDECAR"], "outer-sidecar")
                cancel.assert_called_once()
                program.assert_not_called()

    def test_unnamed_gate_uses_program_memory_admission(self) -> None:
        with PlacementFixture() as fixture:
            program_requests: list[wire.AdmitRequest] = []

            def grant(request: queue_place.RefreshedRequest, _output: client.WaitOutput) -> wire.Admitted:
                program_requests.append(request.refresh())
                return PROGRAM_GRANT

            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}),
                  mock.patch.object(records, "process_start_time", return_value="123"),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(first_step, "_placement_grant") as placement_grant,
                  mock.patch.object(queue_place, "request_place", side_effect=grant),
                  mock.patch.object(queue_place, "release_place", return_value=wire.Ok()) as release,
                  forbid_admission_calls()):
                with first_step.admitted_run(GATE_COMMAND, fixture.root) as admitted:
                    self.assertEqual(len(program_requests), 1)
                    profile = program_requests[0].profile
                    assert isinstance(profile, records.ProgramProfile)
                    self.assertEqual(profile.program, "gate")
                    self.assertGreater(profile.sizing.first_peak_bytes, 0)
                    self.assertEqual(program_requests[0].argv, ("gate",))
                    self.assertFalse(admitted.assignment_file.exists())
                    admitted.handoff(fixture.root)
                    self.assertNotIn("BUILDLOG_PLACEMENT_FILE", os.environ)
                release.assert_called_once_with(PROGRAM_GRANT.reservation, 0, PROGRAM_GRANT.cleanup_timing, "handed_off")
                placement_grant.assert_not_called()

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

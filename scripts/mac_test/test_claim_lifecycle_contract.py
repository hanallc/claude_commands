"""Check claim ownership across the provider, adapter, and admission service."""

from __future__ import annotations

import ast
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import io
import inspect
import textwrap
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock
from typing import Literal, cast, final

from scripts.aws_target import hana_box, hana_execution, hana_placement, hana_transport
from scripts.aws_target.test_hana_box import IDENTITY
from scripts.build_queue import admission_client, admission_protocol as wire, execution_identity, placement
from scripts.build_queue import admission_records as records, admission_service as service
from scripts.build_queue import admission_wire as status_wire
from scripts.build_queue.admission_test_support import CallerProviderControls, private_caller_provider
from scripts.build_queue.test_placement import (
    ManualEvidenceWorker, PlacementFixture, assignment_for, hana_policy_request, placement_request, publish_candidate,
)
from scripts.build_targets import build_targets, target_config
from scripts.copies import copies
from scripts.direct.step_selection import Pinned
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.mac_test import claim_state, eligibility, mac_test, offload
from scripts.mac_test.claim_release_test_support import run_claim_release
from scripts.mac_test.offload_test_commands import capture_fence_query, prepare_probe_fence


BUSY = offload.BusyMacDecline(offload.LostRunLeftovers(), 0)
NO_NATIVE_OPERATION = hana_box.NoOperation()


def reserve_assignment(assignment: placement.PlacementAssignment, root: Path) -> claim_state.HeldClaim:
    args = mac_test.CliArguments()
    args.target, args.pid, args.worktree = assignment.destination.target, os.getpid(), str(root)
    args.what = "wait for the provider"
    paths = claim_state.state_paths(args.target)
    with redirect_stdout(io.StringIO()):
        status = mac_test.command_claim(args, paths, watch_claim=False, assignment=assignment)
    if status != 0:
        raise AssertionError(f"Reservation failed with status {status}")
    held, = claim_state.read_claims(paths)
    return held


@final
class ClaimLifecycleFixture(PlacementFixture):
    def __init__(self, *, target_name: str = "mac") -> None:
        super().__init__()
        self.placement_request = placement_request(self)
        self.settings = self.root / "mac_test.conf"
        self.host = self.placement_request.constraints.candidates[0].destination.host
        _ = write_config_fixture(self.settings, f"offload=on\nhost={self.host}\n", template="mac_test")
        if target_name != "mac":
            _ = cast(object, self.enter_context(mock.patch.dict(os.environ, config_environment(self.root))))
            registry = self.root / "build_targets.conf"
            _ = registry.write_text(f"targets={target_name}\nkind.{target_name}=ssh\n"
                + f"platform.{target_name}=macos\nsettings.{target_name}={self.settings}\n")
            target = target_config.configured_ssh_target(target_name)
            original = self.placement_request.constraints
            candidates = build_targets.fixed_remote_candidates(target, build_targets.CONFIGURED_PROVIDER_SETTINGS,
                eligibility.KnownRepository("hana"), self.root, original.package, "check", original.command,
                build_targets.AnyTarget(), selection=Pinned(target_name))
            candidate, = candidates
            self.placement_request = replace(self.placement_request, constraints=replace(original,
                candidates=(replace(candidate, eligibility=placement.AllowedHost()),)))
        _ = publish_candidate(self, self.placement_request)
        self.assignment = assignment_for(self, self.placement_request)
        self.reference = placement.assigned_claim_reference(self.assignment)
        self.identity = claim_state.ClaimIdentity(self.reference.claim_id, self.reference.episode, self.reference.attempt)
        self.target = self.assignment.destination.target
        self.key = execution_identity.attempt_key(self.reference.episode, self.reference.attempt)
        self.paths = claim_state.state_paths(self.target)
        self.journal = offload.pending_start_path(self.target, self.identity)
        self.controller = offload.ProcessController()
        self.transport = subprocess.run
        self.adapter = offload.OffloadRequest(self.root, "hana", "lifecycle", offload.NO_RESULT_FILE, (),
            owner="fixture-adapter", holder_session=claim_state.session_id(self.assignment.session),
            target=self.target, placement=offload.AssignedOffloadRequest(self.assignment))

    def acquire(self, arguments: Sequence[str], attempt: offload.LocalClaimAttempt) -> None:
        """Run the actual provider command without enabling its fixture watcher."""
        parsed = mac_test.parser().parse_args(arguments[2:], namespace=mac_test.CliArguments())
        output = io.StringIO()
        with redirect_stdout(output):
            status = mac_test.command_claim(parsed, self.paths, watch_claim=False, assignment=self.assignment)
        self.controller.collect_claim_reply(attempt, offload.CommandOutput(status, output.getvalue(), ""))

    def claim(self, test: unittest.TestCase, *, wait: bool = False) -> offload.MacClaimAnswer:
        request = self.adapter
        if wait:
            request = replace(request, admission=offload.MacClaimFromWait(os.getpid(), self.identity, self.assignment.session))
        with mock.patch.object(self.controller, "complete_claim", side_effect=self.acquire):
            answer = offload.claim_for_run(self.controller, eligibility.read_config(self.settings), request,
                                           eligibility.KnownRepository(self.reference.scope.name)
                                           if isinstance(self.reference.scope, claim_state.RepositoryScope)
                                           else eligibility.KnownRepository("hana"))
        if wait:
            test.assertIsInstance(answer, offload.MacClaimNotConfirmed)
        else:
            test.assertIsInstance(answer, offload.FreshMacClaim)
        return answer

    def reserve(self) -> claim_state.HeldClaim:
        return reserve_assignment(self.assignment, self.root)

    def report(self, result: offload.MacOffloadResult = BUSY) -> None:
        with (mock.patch.object(admission_client, "report_execution", side_effect=self.server.handle_request),
              mock.patch.object(admission_client, "claim_failure", side_effect=self.server.handle_request)):
            offload.report_placement_result(self.controller, self.adapter, result)

    def evidence(self, *, filtered: bool = False) -> placement.HostEvidence:
        self.clock.advance(1)
        destination = self.assignment.destination
        key = self.server.host_evidence_key(self.placement_request, destination)
        self.server.host_requests[key] = self.placement_request
        if filtered:
            evidence: placement.HostEvidence = placement.FreshHostEvidence(
                key, self.clock.now(), "", placement.AllowedHost())
        else:
            target = target_config.SshTarget(self.target, "macos", self.settings)
            with (mock.patch.object(target_config, "read_targets", return_value=(target,)),
                  mock.patch.object(build_targets, "ssh_target_eligibility", return_value=target_config.EligibleTarget(target)),
                  mock.patch.object(self.server, "_observe_provider_execution", return_value=execution_identity.ExecutionNotStarted())):
                evidence = self.server.read_provider_evidence(key)
        self.provider_evidence[key] = evidence
        self.server.publish_host_evidence(evidence)
        return evidence

    def check_pending(self, test: unittest.TestCase, *, journal: bool = False) -> None:
        saved = self.server.assignments[self.key]
        test.assertEqual(saved.provider, self.reference)
        test.assertIsInstance(saved.state, placement.CurrentAssignment)
        test.assertEqual(self.server.current_assignments.get(self.assignment.episode), self.key)
        test.assertEqual(self.controller.attempt_evidence.cleanup, "unconfirmed")
        test.assertIsInstance(self.controller.ssh_dispatch_path(), placement.DispatchPathPending)
        test.assertEqual(self.controller.claim_identity, self.identity)
        test.assertEqual(self.journal.exists(), journal)

    def prepare_legacy_fence(self) -> claim_state.HeldClaim:
        held, = claim_state.read_claims(self.paths)
        fence = offload.fence_reference(self.reference.key, self.host, "", held.path.stem)
        command = offload.prepare_start_fence_command(fence)
        _ = self.transport(["bash", "-c", command], check=True, timeout=TEST_HANG_GUARD_SECS)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        _ = self.journal.write_text(offload.pending_start_key(self.identity))
        return held

    def fence_command(self, command: str, *, remote_home: Path | None = None) -> subprocess.CompletedProcess[str]:
        environment = os.environ if remote_home is None else {**os.environ, "HOME": str(remote_home)}
        return self.transport(["bash", "-c", command], env=environment, check=False, capture_output=True, text=True,
                              timeout=TEST_HANG_GUARD_SECS)

    def bind_fence(self) -> claim_state.FenceRef:
        held, = claim_state.read_claims(self.paths)
        with mock.patch.object(self.controller, "capture", return_value=capture_fence_query(offload.FENCE_HOST_QUERY)):
            responder = offload.read_fence_host(self.controller, eligibility.read_config(self.settings))
        assert isinstance(responder, claim_state.FenceHostIdentity), responder
        fence = replace(offload.fence_reference(self.reference.key, self.host, "", held.path.stem), responder=responder)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        _ = self.journal.write_text(fence.journal_text())
        return fence

    def prepare_bound_probe(self, test: unittest.TestCase, *, lose_reply: bool = False) -> claim_state.FenceRef:
        def capture(arguments: Sequence[str], _timeout: offload.CommandTimeLimit) -> offload.CapturedCommandResult:
            command = arguments[-1]
            if command == offload.FENCE_HOST_QUERY:
                return capture_fence_query(command)
            test.assertNotEqual(prepare_probe_fence(command), command)
            if lose_reply:
                return offload.CommandTimedOut()
            return offload.CommandOutput(0, f"free_gib=80\nhost={self.host}\nrc=0\n", "")

        with mock.patch.object(self.controller, "capture", side_effect=capture):
            probe = offload.probe_mac(self.controller, eligibility.read_config(self.settings),
                eligibility.KnownRepository("hana"), self.target)
        test.assertIsInstance(probe, offload.UnavailableProbe if lose_reply else offload.ProbeSnapshot)
        fence = self.controller.fence
        assert isinstance(fence, claim_state.FenceRef)
        return fence

    @staticmethod
    def erase_preparation(fence: claim_state.FenceRef) -> None:
        lock = Path.home() / fence.path
        lock.unlink()
        state = lock.parents[1].with_name("dispatch-fence-state") / lock.parent.name
        (state / lock.with_suffix(".prepared").name).unlink()

    def save_old_closure(self, acquisition: records.ClaimAcquisitionEnded | records.ClaimAcquisitionUnconfirmed) -> None:
        saved = self.server.assignments[self.key]
        report = placement.TerminalAttemptReport(placement.PrelaunchRefused("busy"), placement.DispatchPathSettled(),
                                                  self.clock.now())
        self.server.assignments[self.key] = replace(saved, state=placement.ClosedAssignment("busy"),
            provider=self.reference, terminal_report=report, acquisition=acquisition)
        del self.server.current_assignments[self.assignment.episode]
        self.server.persist()

    def release_exact(self, *, guessed_scope: str = "", remote_home: Path | None = None) -> offload.ClaimReleaseState:
        def ssh(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            if arguments[0] != "ssh" or arguments[-2] != self.host:
                raise AssertionError(f"Unexpected release transport {arguments[:2]}")
            command = arguments[-1]
            if guessed_scope:
                fence = offload.fence_reference(self.reference.key, self.host, "", guessed_scope)
                command = (offload.prepare_start_fence_command(fence) + "\n"
                           + offload.start_fence_command(fence, settle=True)
                           + "\nprintf 'leftovers=0\nrc=0\n'")
            return self.fence_command(command, remote_home=remote_home)

        with (mock.patch.object(subprocess, "run", side_effect=ssh),
              mock.patch.object(self.controller, "capture", side_effect=run_claim_release)):
            return offload.release_claim(self.controller, self.target)


@dataclass(frozen=True)
class HanaClaimLifecycle:
    fixture: PlacementFixture
    controller: hana_box.HanaController
    transport: hana_placement.HanaControlTransport
    assignment: placement.PlacementAssignment
    reference: placement.ProviderClaimReference
    key: str

    @property
    def saved(self) -> records.SavedPlacementAssignment:
        return self.fixture.server.assignments[self.key]

    def record(self, saved: records.SavedPlacementAssignment) -> None:
        self.fixture.server.assignments[self.key] = saved

    def absence(self) -> wire.ExecutionReport:
        return wire.ExecutionReport("no-claim", self.assignment, placement.NoProviderClaim(),
            execution_identity.ExecutionNotStarted(), execution_identity.HanaCommandAdmissionRefused("busy"),
            placement.DispatchPathSettled(claim_state.NoClaimAcquired()))

    def write_native(self, *, claims: Sequence[hana_box.WorkClaim] = (),
                     operation: hana_box.PendingOperation | hana_box.NoOperation = NO_NATIVE_OPERATION) -> None:
        save = cast(Callable[[hana_box.BoxRecord], None], getattr(self.controller, "_save"))
        save(hana_box.BoxRecord(IDENTITY, hana_box.Lifecycle.IDLE_STOPPED, claims=list(claims), operation=operation))

    def write_dispatch(self, release: Callable[[], hana_placement.HanaClaimRelease]) -> None:
        _ = hana_execution.settle_unpublished_claim(self.controller.directory, self.reference,
            release_claim=release, reason="The fixture refused the command")


@contextmanager
def hana_claim_lifecycle() -> Generator[HanaClaimLifecycle]:
    with PlacementFixture() as fixture:
        request, transport = hana_policy_request(fixture)
        request = replace(request, prerequisite=placement.TokenNotRequired())
        controller = hana_box.HanaController(fixture.root / "hana-state", fixture.root / "account.conf",
            transport.settings, hana_box.ProviderNotInstalled())
        with mock.patch.object(hana_placement, "controller_for", return_value=controller):
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            reference = placement.assigned_claim_reference(assignment)
            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            yield HanaClaimLifecycle(fixture, controller, transport, assignment, reference, key)


class ClaimLifecycleContractTests(unittest.TestCase):
    def test_retention_keeps_an_expired_claim_with_a_pending_start(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            _ = fixture.prepare_legacy_fence()
            fixture.save_old_closure(records.ClaimAcquisitionUnconfirmed())
            fixture.proc.kill(wire.Owner(fixture.assignment.owner.pid, fixture.assignment.owner.start_time))
            config = fixture.server.last_usable_configuration
            assert isinstance(config, records.MemoryConfig)
            fixture.clock.advance(config.ended_placement_seconds + 1)
            fixture.server.persist()
            fixture.restart()
            self.assertIn(fixture.key, fixture.server.assignments, "Retention must keep the pending journal's obligation.")

    def test_restart_recovers_a_closed_claim_without_a_live_request(self) -> None:
        with (ClaimLifecycleFixture() as fixture,
              mock.patch.object(service, "HostEvidenceWorker", ManualEvidenceWorker)):
            fixture.save_old_closure(acquisition=records.ClaimAcquisitionEnded(fixture.clock.now()))
            fixture.proc.kill(wire.Owner(fixture.assignment.owner.pid, fixture.assignment.owner.start_time))
            del fixture.server.placement_requests[fixture.assignment.episode]
            fixture.server.persist()
            fixture.restart()
            config = fixture.server.last_usable_configuration
            assert isinstance(config, records.MemoryConfig)
            fixture.clock.advance(config.ended_placement_seconds / 2)
            fixture.server.host_probe = fixture.read_host_evidence
            _ = fixture.run_pass()
            worker = fixture.server.host_targets.get((fixture.reference.provider, fixture.target))
            self.assertIsInstance(worker, ManualEvidenceWorker, "Recovery must subscribe a closed obligation.")
            assert isinstance(worker, ManualEvidenceWorker)
            _ = worker.probe_next()
            _ = fixture.run_pass()
            fixture.restart()
            status, = fixture.server.handle_request(status_wire.PlacementStatusRequest(
                fixture.assignment.episode, fixture.assignment.attempt))
            self.assertEqual(status, status_wire.PlacementStatusReply(status_wire.KnownPlacementStatus(fixture.assignment,
                placement.ClosedAssignment("busy"), fixture.reference)))
            fixture.clock.advance(config.ended_placement_seconds)
            fixture.server.persist()
            self.assertNotIn(fixture.key, fixture.server.assignments)

    def test_missing_preparation_records_do_not_release_a_prepared_fence(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            fence = fixture.prepare_bound_probe(self)
            fixture.erase_preparation(fence)
            self.assertIsInstance(fixture.release_exact(), offload.ClaimReleaseUnconfirmed,
                                  "Missing remote records cannot release a prepared fence.")
            self.assertTrue(fixture.journal.exists())

    def test_lost_preparation_reply_keeps_the_journal_unknown(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            fence = fixture.prepare_bound_probe(self, lose_reply=True)
            journal = offload.read_start_fence(fence)
            fixture.erase_preparation(fence)
            self.assertIsInstance(fixture.release_exact(), offload.ClaimReleaseUnconfirmed,
                                  "A lost preparation reply must retain the claim when remote records are absent.")
            self.assertEqual(journal, claim_state.StartFenceJournal(fence, claim_state.FencePreparation.UNKNOWN))

    def test_legacy_bound_journal_without_preparation_state_is_unknown(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            _ = fixture.bind_fence()
            self.assertIsInstance(fixture.release_exact(), offload.ClaimReleaseUnconfirmed,
                                  "Missing preparation history cannot authorize cancellation.")
            self.assertTrue(fixture.journal.exists())

    def test_remote_start_cannot_settle_a_prepared_fence_with_missing_records(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            fence = fixture.prepare_bound_probe(self)
            fixture.erase_preparation(fence)
            reply = fixture.fence_command(offload.fenced_start_command(fence, "printf 'launched\n'"))
            self.assertEqual(reply.returncode, 75)
            self.assertNotIn("launched", reply.stdout)
            self.assertNotIn(fence.acknowledgement("settled"), reply.stdout,
                             "The start command cannot settle absent preparation records.")

    def test_saved_settled_report_keeps_the_claim_without_release_proof(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            _ = fixture.prepare_legacy_fence()
            report = placement.TerminalAttemptReport(placement.PrelaunchRefused("busy"),
                placement.DispatchPathSettled(), fixture.clock.now())
            saved = fixture.server.assignments[fixture.key]
            fixture.server.assignments[fixture.key] = replace(saved, provider=fixture.reference, terminal_report=report)
            fixture.server.persist()
            fixture.restart()
            _ = fixture.evidence()
            _ = fixture.run_pass()
            self.assertEqual(fixture.server.current_assignments.get(fixture.assignment.episode), fixture.key,
                             "A saved report without release proof must keep the claim current.")

    def test_retained_start_journal_blocks_a_new_assignment_without_a_claim_record(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            held = fixture.prepare_legacy_fence()
            held.path.unlink()
            saved = fixture.server.assignments[fixture.key]
            report = placement.TerminalAttemptReport(placement.PrelaunchRefused("busy", placement.PrelaunchEnded()),
                                                     placement.DispatchPathSettled(), fixture.clock.now())
            fixture.server.assignments[fixture.key] = replace(saved, provider=fixture.reference, terminal_report=report,
                state=placement.ClosedAssignment("busy"))
            del fixture.server.current_assignments[fixture.assignment.episode]
            fixture.clock.advance(1)
            other = placement_request(fixture)
            _ = publish_candidate(fixture, other, claims=())
            reply, = fixture.server.handle_request(other)
            self.assertIsInstance(reply, wire.PlacementWait, "A retained journal must exclude new work.")
            fixture.journal.unlink()
            self.assertEqual(assignment_for(fixture, other).destination, fixture.assignment.destination)

    def test_settled_dispatch_without_release_proof_keeps_the_refusal_pending(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            held = fixture.prepare_legacy_fence()
            fixture.report()
            _ = fixture.server.handle_request(wire.ExecutionReport("settled-without-release", fixture.assignment,
                fixture.reference, execution_identity.ExecutionNotStarted(), placement.PrelaunchRefused("busy"),
                placement.DispatchPathSettled()))
            fixture.restart()
            _ = fixture.evidence()
            _ = fixture.run_pass()
            self.assertEqual(fixture.server.current_assignments.get(fixture.assignment.episode), fixture.key,
                             "Dispatch settlement without exact release must keep the refusal current.")
            fixture.check_pending(self, journal=True)
            self.assertTrue(held.path.is_file())

    def test_replacement_host_cannot_acknowledge_the_original_fence(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            held, = claim_state.read_claims(fixture.paths)
            fence = offload.fence_reference(fixture.reference.key, fixture.host, "", held.path.stem)
            _ = fixture.transport(["bash", "-c", offload.prepare_start_fence_command(fence)],
                                  check=True, timeout=TEST_HANG_GUARD_SECS)
            fixture.journal.parent.mkdir(parents=True, exist_ok=True)
            _ = fixture.journal.write_text(fence.journal_text())
            replacement = fixture.root / "replacement-host"
            replacement.mkdir()
            release = fixture.release_exact(remote_home=replacement)
            self.assertIsInstance(release, offload.ClaimReleaseUnconfirmed,
                                  "The replacement host has no record of the original fence.")
            self.assertTrue(fixture.journal.is_file())
            self.assertTrue(held.path.is_file())
            self.assertTrue((fixture.root / fence.path).is_file())
            state = fence.path.parents[2]
            _ = shutil.copytree(fixture.root / state, replacement / state)
            self.assertIsInstance(fixture.release_exact(remote_home=replacement), offload.ClaimReleaseUnconfirmed)
            for wrong in (replace(fence, host="other-host"), replace(fence, generation="other-generation")):
                answer = fixture.transport(["bash", "-c", offload.start_fence_command(wrong, settle=True)],
                    check=False, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(answer.returncode, 75, answer.stdout + answer.stderr)
                self.assertNotIn(wrong.acknowledgement("settled"), answer.stdout)
            self.assertIsInstance(fixture.release_exact(), offload.ClaimReleaseConfirmed)

    def test_caller_fixtures_read_the_named_mac_claim(self) -> None:
        with ClaimLifecycleFixture(target_name="mac-spare") as fixture:
            held = fixture.reserve()
            environment = {**os.environ, "BUILD_TARGETS_STATE_DIR": str(fixture.paths.directory.parent),
                           "MAC_TEST_STATE_DIR": str(fixture.root / "default-mac")}
            fixture.caller_environment = environment
            controls = CallerProviderControls(fixture)
            key = fixture.server.host_evidence_key(fixture.placement_request, fixture.assignment.destination)
            with self.subTest(reader="controls"):
                self.assertEqual(controls.claim_paths(fixture.target).platform, fixture.paths.platform,
                                 "Caller controls must use the named target platform.")
                self.assertEqual(controls.claims(fixture.target), (held,))
            with self.subTest(reader="private provider"):
                evidence = private_caller_provider(fixture, environment, key)
                assert isinstance(evidence, placement.FreshHostEvidence)
                self.assertIn(fixture.reference, evidence.held_claims,
                              "The private provider must read the named target state.")

    def test_named_mac_release_settles_the_placement_claim(self) -> None:
        with ClaimLifecycleFixture(target_name="mac-spare") as fixture:
            _ = fixture.claim(self)
            observed = fixture.evidence()
            self.assertIsInstance(observed, placement.FreshHostEvidence)
            assert isinstance(observed, placement.FreshHostEvidence)
            self.assertIn(fixture.reference, observed.held_claims)
            fixture.report()
            self.assertIsInstance(fixture.release_exact(), offload.ClaimReleaseConfirmed)
            fixture.report()
            self.assertIsInstance(fixture.server.assignments[fixture.key].state, placement.ClosedAssignment,
                                  "Admission must accept the named Mac's exact release receipt.")

    def test_missing_legacy_fence_does_not_acknowledge_an_unverified_release(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            held = fixture.prepare_legacy_fence()
            fence = offload.fence_reference(fixture.reference.key, fixture.host, "", held.path.stem)
            _ = fixture.journal.write_text(fence.journal_text())
            (Path.home() / fence.path).unlink()
            self.assertIsInstance(fixture.release_exact(), offload.ClaimReleaseUnconfirmed,
                                  "An unexplained missing fence must not confirm release.")

    def test_bound_cancellation_replays_after_delayed_preparation(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            fence = fixture.bind_fence()
            prepare = offload.prepare_start_fence_command(fence)
            journal = claim_state.StartFenceJournal(fence, claim_state.FencePreparation.NOT_SENT)
            offload.write_start_journal(journal)
            settle = offload.start_fence_command(fence, settle=True, preparation=journal.preparation)
            replacement = fixture.root / "replacement-host"
            replacement.mkdir()
            self.assertNotEqual(fixture.fence_command(prepare, remote_home=replacement).returncode, 0)
            self.assertNotEqual(fixture.fence_command(settle, remote_home=replacement).returncode, 0)
            # Lose the first reply after the remote record is durable.
            first = fixture.fence_command(settle)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn(fence.acknowledgement("settled"), first.stdout)
            self.assertEqual(fixture.fence_command(prepare).returncode, 75)
            self.assertFalse((Path.home() / fence.path).exists())
            self.assertIsInstance(fixture.release_exact(), offload.ClaimReleaseConfirmed)
            self.assertFalse(fixture.journal.exists())

    def test_bound_preparation_replays_only_a_durable_settlement(self) -> None:
        for lost_lock in (False, True):
            with self.subTest(lost_lock=lost_lock), ClaimLifecycleFixture() as fixture:
                _ = fixture.claim(self)
                fence = fixture.bind_fence()
                prepared = fixture.fence_command(offload.prepare_start_fence_command(fence))
                self.assertEqual(prepared.returncode, 0, prepared.stderr)
                if lost_lock:
                    (Path.home() / fence.path).unlink()
                    self.assertIsInstance(fixture.release_exact(), offload.ClaimReleaseUnconfirmed)
                    self.assertTrue(fixture.journal.exists())
                else:
                    settled = fixture.fence_command(offload.start_fence_command(fence, settle=True))
                    self.assertEqual(settled.returncode, 0, settled.stderr)
                    self.assertIsInstance(fixture.release_exact(), offload.ClaimReleaseConfirmed)
                    self.assertFalse(fixture.journal.exists())

    def test_round_eight_unbound_lock_reports_the_unverified_host(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            held = fixture.prepare_legacy_fence()
            fence = offload.fence_reference(fixture.reference.key, fixture.host, "", held.path.stem)
            lock = Path.home() / fence.path
            _ = lock.write_text("")
            reply = fixture.fence_command(offload.start_fence_command(fence, settle=True))
            self.assertEqual(reply.returncode, 75)
            self.assertIn("The legacy start fence has no verified host binding.", reply.stderr)
            self.assertNotIn(fence.acknowledgement("settled"), reply.stdout)
            self.assertTrue(lock.exists())
            self.assertTrue(fixture.journal.exists())

    def test_later_launch_end_evidence_survives_cancellation_and_lost_receipt(self) -> None:
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled), ClaimLifecycleFixture() as fixture:
                _ = fixture.claim(self)
                report = wire.ExecutionReport("prelaunch-pending", fixture.assignment, fixture.reference,
                    execution_identity.ExecutionNotStarted(), placement.PrelaunchRefused("busy"),
                    placement.DispatchPathPending("the adapter has not settled"))
                _ = fixture.server.handle_request(report)
                _ = fixture.evidence()
                ended = replace(report, request_id="prelaunch-ended",
                                outcome=placement.PrelaunchRefused("busy", placement.PrelaunchEnded()))
                _ = fixture.server.handle_request(ended)
                if cancelled:
                    _ = fixture.server.handle_request(wire.CancelPlacement(
                        "cancel", fixture.assignment.episode, fixture.assignment.owner, "stopped"))
                fixture.restart()
                self.assertIsInstance(fixture.server.assignments[fixture.key].state, placement.CurrentAssignment)
                self.assertIsInstance(fixture.release_exact(), offload.ClaimReleaseConfirmed)
                # The client has no release receipt. Only the next inventory reports the absence.
                _ = fixture.evidence()
                _ = fixture.run_pass()
                expected = "cancelled" if cancelled else "busy"
                self.assertEqual(fixture.server.assignments[fixture.key].state, placement.ClosedAssignment(expected),
                                 "Later launch end evidence must allow release recovery.")

    def test_hana_absence_reports_use_the_wire_and_reject_claim_conflicts(self) -> None:
        for conflict in ("none", "record", "guard", "saved claim", "consumed permission", "pending refusal"):
            with self.subTest(conflict=conflict), hana_claim_lifecycle() as hana:
                saved = hana.saved
                if conflict == "record":
                    _ = reserve_assignment(hana.assignment, hana.fixture.root)
                if conflict == "guard":
                    hana.controller.bind_execution(hana.reference.claim_id)
                if conflict == "saved claim":
                    saved = replace(saved, provider=hana.reference)
                if conflict == "consumed permission":
                    saved = replace(saved, permission=placement.PermissionConsumed(hana.fixture.clock.now()))
                if conflict == "pending refusal":
                    saved = replace(saved, terminal_report=placement.TerminalAttemptReport(
                        placement.PrelaunchRefused("earlier refusal"), placement.DispatchPathPending("awaiting proof"),
                        hana.fixture.clock.now()))
                hana.record(saved)
                refusal = hana.absence()
                encoded = wire.encode_request(refusal)
                self.assertEqual(wire.parse_request(encoded), refusal)
                _ = hana.fixture.connect(encoded).replies()
                after = hana.saved
                if conflict in ("none", "pending refusal"):
                    self.assertIsInstance(after.state, placement.ClosedAssignment)
                    self.assertEqual(after.claim_release, records.ProviderClaimReleased(claim_state.NoClaimAcquired()),
                        "Confirmed absence must settle the earlier unbound refusal.")
                else:
                    self.assertEqual(after, saved, "A claim conflict must reject absence evidence.")

    def test_hana_absence_settles_the_path_without_replacing_cancellation(self) -> None:
        with hana_claim_lifecycle() as hana:
            terminal = placement.TerminalAttemptReport(placement.AttemptCancelled("coordinator cancelled"),
                placement.DispatchPathPending("awaiting proof"), hana.fixture.clock.now())
            hana.record(replace(hana.saved, terminal_report=terminal))
            hana.fixture.clock.advance(1)
            absence = hana.absence()
            _ = hana.fixture.connect(absence).replies()
            self.assertEqual(hana.saved.terminal_report, replace(terminal, path=absence.path),
                "An absence report must preserve the stored cancellation.")
            self.assertEqual(hana.saved.state, placement.ClosedAssignment("cancelled"))
            self.assertEqual(hana.saved.claim_release, records.ProviderClaimReleased(claim_state.NoClaimAcquired()))

    def test_saved_release_settles_the_path_without_replacing_cancellation(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            terminal = placement.TerminalAttemptReport(placement.AttemptCancelled("coordinator cancelled"),
                placement.DispatchPathPending("awaiting proof"), fixture.clock.now())
            proof = claim_state.AuthoritativeClaimAbsence(fixture.reference.key)
            saved = fixture.server.assignments[fixture.key]
            fixture.server.assignments[fixture.key] = replace(saved, provider=fixture.reference,
                terminal_report=terminal, claim_release=records.ProviderClaimReleased(proof))
            fixture.clock.advance(1)
            _ = fixture.run_pass()
            saved = fixture.server.assignments[fixture.key]
            self.assertEqual(saved.terminal_report, replace(terminal, path=placement.DispatchPathSettled(proof)),
                "Saved release evidence must preserve the stored cancellation.")
            self.assertEqual(saved.state, placement.ClosedAssignment("cancelled"))

    def test_hana_release_confirmation_uses_absence_proof_without_the_refusal_outcome(self) -> None:
        for outcome in (placement.AttemptCancelled("cancelled"), placement.PrelaunchRefused("earlier refusal")):
            with self.subTest(outcome=outcome), hana_claim_lifecycle() as hana:
                proof = claim_state.NoClaimAcquired()
                terminal = placement.TerminalAttemptReport(outcome, placement.DispatchPathSettled(proof),
                    hana.fixture.clock.now())
                hana.record(replace(hana.saved, terminal_report=terminal))
                _ = hana.fixture.run_pass()
                self.assertEqual(hana.saved.claim_release, records.ProviderClaimReleased(proof),
                    "Absence proof must confirm release independently of the stored outcome.")
                self.assertEqual(hana.saved.terminal_report, terminal)

    def test_hana_absence_rejects_native_obligations_and_accepts_a_settled_journal(self) -> None:
        for record in ("native claim", "operation", "unreadable", "settled"):
            with self.subTest(record=record), hana_claim_lifecycle() as hana:
                execution = hana.reference.claim_id
                now = int(hana.fixture.clock.now())
                if record == "native claim":
                    hana.write_native(claims=[hana_box.WorkClaim(execution, "claim-token", now)])
                elif record == "operation":
                    hana.write_native(operation=hana_box.PendingOperation(hana_box.Action.START, "start-token",
                        now, 1, execution=execution))
                elif record == "unreadable":
                    hana.write_native()
                    _ = hana.controller.path.write_text("{")
                else:
                    hana.controller.bind_execution(execution)
                    hana.write_dispatch(hana_placement.HanaClaimReleased)
                saved = hana.saved
                _ = hana.fixture.connect(hana.absence()).replies()
                if record == "settled":
                    self.assertEqual(hana.saved.claim_release, records.ProviderClaimReleased(claim_state.NoClaimAcquired()),
                        "A settled journal and its guard must not retain a released claim.")
                else:
                    self.assertEqual(hana.saved, saved, "Native or unreadable evidence must reject absence.")

    def test_hana_restore_and_start_share_open_release_evidence(self) -> None:
        with mock.patch.object(service, "HostEvidenceWorker", ManualEvidenceWorker):
            for record in ("native operation", "settled", "unsettled"):
                with self.subTest(record=record), hana_claim_lifecycle() as hana:
                    if record == "native operation":
                        operation = hana_box.PendingOperation(hana_box.Action.START, "start-token",
                            int(hana.fixture.clock.now()), 1, execution=hana.reference.claim_id)
                        hana.write_native(operation=operation)
                    elif record == "settled":
                        hana.controller.bind_execution(hana.reference.claim_id)
                        hana.write_dispatch(hana_placement.HanaClaimReleased)
                    else:
                        hana.write_dispatch(lambda: hana_placement.HanaClaimReleaseRefused("The claim remains held"))
                    if record == "unsettled":
                        server = hana.fixture.server
                        pending = cast(Callable[[records.SavedPlacementAssignment], bool], getattr(server, "_start_pending"))
                        saved = hana.saved
                        self.assertTrue(pending(saved))
                        worker = server.host_targets[("ssh", hana.reference.target)]
                        assert isinstance(worker, ManualEvidenceWorker)
                        _ = worker.probe_next()
                        self.assertTrue(pending(saved), "An unsettled journal alone must retain the start.")
                    hana.fixture.server.persist()
                    hana.fixture.restart()
                    expected = placement.NoProviderClaim() if record == "settled" else hana.reference
                    self.assertEqual(hana.saved.provider, expected,
                        "Restore must bind only an outstanding release obligation.")

    def check_hana_prelaunch_refusal(self, kind: Literal["input", "provider", "resources"], *, retained: bool) -> None:
        with hana_claim_lifecycle() as hana:
            fixture, lifecycle, transport = hana.fixture, hana.controller, hana.transport
            assignment, reference = hana.assignment, hana.reference
            adapter = offload.ProcessController()
            step = offload.OffloadRequest(fixture.root, "hana", "fixture", offload.NO_RESULT_FILE, (),
                owner="fixture-adapter", holder_session=claim_state.session_id(assignment.session), target=reference.target, verb="check", kind="check",
                placement=offload.AssignedOffloadRequest(assignment))
            if retained and kind == "provider":
                _ = reserve_assignment(assignment, fixture.root)
            elif retained:
                lifecycle.bind_execution(reference.claim_id)
            reason = "The fixture refuses the claim"
            claim = (hana_placement.HanaClaimResourcesHeld() if kind == "resources"
                     else hana_placement.HanaClaimRefused(reason))
            with (mock.patch.object(hana_placement, "validate_assignment", return_value=transport),
                  mock.patch.object(hana_placement, "claim_assigned", return_value=claim) as acquire,
                  mock.patch.object(hana_transport, "check_inputs",
                                    side_effect=ValueError("invalid source") if kind == "input" else None),
                  mock.patch.object(hana_placement, "release_assigned",
                                    return_value=hana_placement.HanaClaimReleaseRefused("claim remains held")) as release,
                  mock.patch.object(admission_client, "report_execution") as report,
                  mock.patch.object(admission_client, "claim_failure") as failure):
                outcome = offload.claimed_run(adapter, transport, step, eligibility.KnownRepository("hana"), 0)
                self.assertIsInstance(outcome, offload.HanaCommandAdmissionRefused)
                self.assertEqual(acquire.call_count, int(kind != "input"),
                    "Input refusal must stop before claim acquisition.")
                offload.report_placement_result(adapter, step, outcome)
            self.assertEqual(report.call_count, 1, "The refusal must report its claim evidence.")
            failure.assert_not_called()
            sent = cast(wire.ExecutionReport, report.call_args.args[0])
            self.assertEqual(wire.parse_request(wire.encode_request(sent)), sent)
            self.assertIsInstance(sent.outcome, execution_identity.HanaCommandAdmissionRefused)
            self.assertIsInstance(sent.evidence, execution_identity.ExecutionNotStarted)
            journal = hana_execution.execution_path(lifecycle.directory, reference.claim_id)
            if retained:
                self.assertEqual(sent.claim, reference, "A retained claim must keep its exact reference.")
                self.assertIsInstance(sent.path, placement.DispatchPathPending)
                self.assertEqual(release.call_count, int(kind == "provider"))
                self.assertEqual(journal.is_file(), kind == "provider")
            else:
                self.assertIsInstance(sent.claim, placement.NoProviderClaim)
                self.assertEqual(sent.path, placement.DispatchPathSettled(claim_state.NoClaimAcquired()))
                release.assert_not_called()
                self.assertFalse(journal.exists())

    def test_hana_input_refusal_does_not_acquire_a_claim(self) -> None:
        for retained in (False, True):
            with self.subTest(retained=retained):
                self.check_hana_prelaunch_refusal("input", retained=retained)

    def test_hana_provider_refusal_preserves_a_saved_claim(self) -> None:
        for retained in (False, True):
            with self.subTest(retained=retained):
                self.check_hana_prelaunch_refusal("provider", retained=retained)

    def test_hana_resource_refusal_reports_a_pending_claim(self) -> None:
        for retained in (False, True):
            with self.subTest(retained=retained):
                self.check_hana_prelaunch_refusal("resources", retained=retained)

    def test_inherited_refusals_use_one_fence_check(self) -> None:
        for function in (offload.run_offload, offload.report_placement_result):
            tree = ast.parse(inspect.getsource(function))
            body = tree.body[0]
            assert isinstance(body, ast.FunctionDef)
            checks = [node for node in ast.walk(body) if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Name) and node.func.id == "retained_refusal_claim"]
            self.assertEqual(len(checks), 1, "All refusal paths must use the common fence check.")
            check, = checks
            for statement in body.body:
                if isinstance(statement, ast.Assign) and statement.value is check:
                    break
                self.assertFalse(any(isinstance(node, ast.Return) for node in ast.walk(statement)),
                    "A refusal path returns before the common fence check.")
            else:
                self.fail("The common fence check must run outside all branches.")

    def test_structure_rejects_an_early_busy_refusal(self) -> None:
        source = inspect.getsource(offload.report_placement_result)
        tree = ast.parse(source)
        body = tree.body[0]
        assert isinstance(body, ast.FunctionDef)
        lines = source.splitlines(keepends=True)
        insertion = body.body[0].end_lineno
        assert insertion is not None
        lines.insert(insertion, textwrap.indent("if isinstance(result, BusyMacDecline):\n    return\n", "    "))
        with mock.patch.object(inspect, "getsource", side_effect=[inspect.getsource(offload.run_offload), "".join(lines)]):
            with self.assertRaisesRegex(AssertionError, "returns before the common fence check"):
                self.test_inherited_refusals_use_one_fence_check()

    def check_inherited_refusal(self, kind: Literal["source", "version", "copy", "leases"], reason: str) -> None:
        with ClaimLifecycleFixture() as fixture:
            held = fixture.reserve()
            wait = offload.MacClaimFromWait(os.getpid(), fixture.identity, fixture.assignment.session)
            request = replace(fixture.adapter, admission=wait)

            def publish_fence() -> None:
                fixture.controller.adopt_claim(fixture.identity)
                fixture.controller.pending = offload.NoPendingOperation()
                fixture.journal.parent.mkdir(parents=True, exist_ok=True)
                _ = fixture.journal.write_text(offload.pending_start_key(fixture.identity))

            def source(*_args: object) -> bool:
                if kind == "source":
                    publish_fence()
                return kind != "source"

            def version(*_args: object) -> str:
                if kind == "version":
                    publish_fence()
                    return "fixture version mismatch"
                return ""

            def lease(_mirror: copies.RemoteMirror) -> None:
                publish_fence()
                if kind == "leases":
                    raise copies.BusyRemoteMirror("the mirror is occupied")
                raise copies.CopyError("the mirror copy failed")

            with (mock.patch.object(eligibility, "mac_step_eligibility", return_value=eligibility.KnownRepository("hana")),
                  mock.patch.object(target_config, "uses_legacy_mac_layout", return_value=False),
                  mock.patch.object(offload, "trusted_linux_source", side_effect=source),
                  mock.patch.object(offload, "recorded_linux_version_mismatch", side_effect=version),
                  mock.patch.object(copies, "remote_lease", side_effect=lease)):
                result = offload.run_offload(fixture.controller, eligibility.read_config(fixture.settings), request, 0)
            self.assertIsInstance(result, (offload.DeclinedMacRun, offload.BusyMacDecline))
            assert isinstance(result, (offload.DeclinedMacRun, offload.BusyMacDecline))
            self.assertEqual(result.reason, reason)
            fixture.controller.pending = offload.NoPendingOperation()
            with (mock.patch.object(admission_client, "claim_failure") as refusal,
                  mock.patch.object(admission_client, "report_execution") as report):
                offload.report_placement_result(fixture.controller, request, result)
            self.assertEqual(refusal.call_count, 1, "The refusal must report the inherited pending fence.")
            sent = cast(wire.ClaimFailure, refusal.call_args.args[0])
            self.assertEqual((sent.assignment, sent.claim, sent.reason), (fixture.assignment, fixture.reference, reason))
            report.assert_not_called()
            self.assertIsInstance(fixture.controller.pending, offload.PendingStartOperation)
            self.assertTrue(fixture.journal.is_file())
            self.assertTrue(held.path.is_file())

    def test_inherited_source_refusal_checks_the_fence(self) -> None:
        self.check_inherited_refusal("source", "repo")

    def test_inherited_version_refusal_checks_the_fence(self) -> None:
        self.check_inherited_refusal("version", "state")

    def test_inherited_copy_refusal_checks_the_fence(self) -> None:
        self.check_inherited_refusal("copy", "copy")

    def test_inherited_lease_refusal_checks_the_fence(self) -> None:
        self.check_inherited_refusal("leases", "busy")

    def test_early_refusal_reports_the_inherited_pending_fence(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.reserve()
            held = fixture.prepare_legacy_fence()
            journal = fixture.journal.read_bytes()
            reservation = offload.MacClaimFromWait(os.getpid(), fixture.identity, fixture.assignment.session)
            successor = replace(fixture.assignment, attempt="successor")
            request = replace(fixture.adapter, admission=reservation,
                              placement=offload.AssignedOffloadRequest(successor))
            with mock.patch.object(eligibility, "mac_step_eligibility",
                                   return_value=eligibility.IneligibleMacStep("off")):
                result = offload.run_offload(fixture.controller, eligibility.read_config(fixture.settings), request, 0)
            with (mock.patch.object(admission_client, "claim_failure") as refusal,
                  mock.patch.object(admission_client, "report_execution") as report):
                offload.report_placement_result(fixture.controller, request, result)
            self.assertEqual(refusal.call_count, 1, "An early refusal must report the inherited pending fence.")
            sent = cast(wire.ClaimFailure, refusal.call_args.args[0])
            self.assertEqual((sent.assignment, sent.claim, sent.reason), (successor, fixture.reference, "off"))
            report.assert_not_called()
            self.assertIsInstance(fixture.controller.pending, offload.PendingStartOperation)
            self.assertEqual(fixture.journal.read_bytes(), journal)
            self.assertTrue(held.path.exists())

    def test_refused_adoption_keeps_the_wait_owner_and_dispatch_pending(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            held = fixture.reserve()
            original = held.path.read_bytes()
            now = datetime.now(UTC)
            block = claim_state.ActiveMacBlock("other-session", "fixture block", now, now + timedelta(hours=1),
                claim_state.RecordedSession("other-session", "other holder"), claim_state.NoShowrunner(),
                "not_configured", claim_state.FreeMessageNotNeeded())
            mac_test.write_block(fixture.paths.block, block)
            answer = fixture.claim(self, wait=True)
            assert isinstance(answer, offload.MacClaimNotConfirmed)
            self.assertEqual(answer.reason, "blocked")
            fixture.report(offload.DeclinedMacRun("blocked", 0))
            _ = fixture.evidence()
            _ = fixture.run_pass()
            self.assertEqual(held.path.read_bytes(), original)
            fixture.check_pending(self)

    def test_incomplete_evidence_keeps_the_exact_claim_current(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            fixture.report()
            _ = fixture.evidence(filtered=True)
            _ = fixture.run_pass()
            self.assertEqual(len(claim_state.read_claims(fixture.paths)), 1)
            fixture.check_pending(self)

    def test_unreadable_evidence_keeps_the_exact_claim_current(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            fixture.report()
            held, = claim_state.read_claims(fixture.paths)
            original = held.path.read_bytes()
            _ = held.path.write_text("unreadable claim")
            try:
                self.assertIsInstance(fixture.evidence(), placement.HostEvidenceUnavailable)
                _ = fixture.run_pass()
                fixture.check_pending(self)
            finally:
                _ = held.path.write_bytes(original)

    def test_acknowledgement_for_guessed_scope_keeps_the_original_fence(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            fixture.report()
            held = fixture.prepare_legacy_fence()
            fence = Path.home() / offload.start_fence_relative(
                fixture.identity, fixture.target, scope_key=held.path.stem).with_suffix(".lock")
            release = fixture.release_exact(guessed_scope="another-record")
            self.assertTrue(fixture.journal.is_file(), "The guessed fence cannot clear the exact journal.")
            self.assertIsInstance(release, offload.ClaimReleaseUnconfirmed)
            self.assertTrue(held.path.is_file())
            self.assertTrue(fence.is_file())
            _ = fixture.evidence()
            _ = fixture.run_pass()
            fixture.check_pending(self, journal=True)

    def test_restart_between_journal_and_claim_removal_keeps_the_obligation(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            fixture.report()
            held = fixture.prepare_legacy_fence()
            unlink = Path.unlink

            def interrupt(path: Path, missing_ok: bool = False) -> None:
                if path == held.path:
                    raise OSError("fixture interrupted the final claim removal")
                unlink(path, missing_ok=missing_ok)

            with (mock.patch.object(Path, "unlink", autospec=True, side_effect=interrupt),
                  self.assertRaisesRegex(OSError, "fixture interrupted")):
                _ = fixture.release_exact()
            self.assertFalse(fixture.journal.exists())
            self.assertTrue(held.path.is_file())
            fixture.restart()
            _ = fixture.evidence()
            _ = fixture.run_pass()
            fixture.check_pending(self)
            self.assertIsInstance(fixture.release_exact(), offload.ClaimReleaseConfirmed)
            fixture.report()
            _ = fixture.evidence()
            _ = fixture.run_pass()
            self.assertFalse(held.path.exists())
            self.assertEqual(fixture.controller.attempt_evidence.cleanup, "confirmed")
            self.assertIsInstance(fixture.controller.ssh_dispatch_path(), placement.DispatchPathSettled)
            self.assertIsInstance(fixture.server.assignments[fixture.key].state, placement.ClosedAssignment)

    def test_stale_execution_report_keeps_the_replacement_claim_and_routing(self) -> None:
        with ClaimLifecycleFixture() as fixture:
            _ = fixture.claim(self)
            fixture.report()
            self.assertIsInstance(fixture.release_exact(), offload.ClaimReleaseConfirmed)
            fixture.report()
            _ = fixture.evidence()
            _ = fixture.run_pass()
            previous = fixture.assignment
            old_evidence = fixture.server.execution_evidence.get(fixture.key)
            replacement = assignment_for(fixture, fixture.placement_request)
            self.assertNotEqual(replacement.attempt, previous.attempt)
            fixture.assignment = replacement
            held = fixture.reserve()
            replacement_key = execution_identity.attempt_key(replacement.episode, replacement.attempt)
            release = wire.ExecutionReport("stale-old", previous, fixture.reference,
                execution_identity.ExecutionEvidenceUnavailable("stale adapter", fixture.clock.now()),
                placement.PrelaunchRefused("busy"), placement.DispatchPathPending("stale adapter"))
            _ = fixture.server.handle_request(release)
            self.assertEqual(fixture.server.current_assignments[replacement.episode], replacement_key)
            self.assertTrue(held.path.is_file())
            self.assertEqual(fixture.server.execution_evidence.get(fixture.key), old_evidence)

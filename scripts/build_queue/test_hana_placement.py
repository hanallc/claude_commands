"""Check boot failure settlement and queue read invalidation through private sockets."""
from __future__ import annotations

from dataclasses import replace
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
import threading
import time
from typing import cast, override
import unittest
from unittest import mock

from scripts.build_queue import admission_protocol as wire, admission_records as records, admission_service as service, placement
from scripts.build_queue.test_placement import (
    PlacementFixture, ManualEvidenceWorker, placement_request, publish_candidate, assignment_for, adopt_assignment, local_candidate,
)
from scripts.build_queue.admission_test_support import only_admitted
from scripts.aws_target import hana_box, hana_execution, hana_placement
from scripts.aws_target.controller_lock import control_lock
from scripts.build_targets import target_config
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


def hana_target(fixture: PlacementFixture) -> target_config.SshTarget:
    settings = fixture.root / 'hana.conf'
    _ = settings.write_text('execution_transport=hana-control\nhost=fixture-hana\n# whole: 1..\n'
        + 'aws_timeout_s=7\n# whole: 1..\nstep_memory_gib=24\n')
    return target_config.SshTarget('fixture-hana', 'linux', settings)


class HanaQueueTests(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        # Load the target helpers before a test replaces the configuration reader.
        _ = import_module('scripts.build_targets.build_targets')

    def test_failed_boot_reports_terminal_outcome_before_capacity_and_fallback_release(self) -> None:
        with PlacementFixture() as fixture:
            target = hana_target(fixture)
            request = placement_request(fixture)
            remote = replace(request.constraints.candidates[0], destination=replace(
                request.constraints.candidates[0].destination, provider='ssh', target=target.name, host='fixture-hana'))
            request = replace(request, prerequisite=placement.TokenNotRequired(),
                constraints=replace(request.constraints, candidates=(remote, local_candidate(fixture))))
            with mock.patch.object(target_config, 'read_targets', return_value=(target,)):
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                reference = adopt_assignment(fixture, request, assignment)
                report = wire.ExecutionReport('boot-failed', assignment, reference, placement.ExecutionNotStarted(),
                    placement.PrelaunchRefused('boot failed'), placement.DispatchPathPending('start recovery'))
                reply = fixture.connect(report).replies()[-1]
                self.assertIsInstance(reply, wire.DispatchStatus)
                key = placement.attempt_key(assignment.episode, assignment.attempt)
                pending = fixture.server.assignments[key]
                self.assertIsInstance(pending.state, placement.CurrentAssignment)
                self.assertIsInstance(pending.terminal_report, placement.TerminalAttemptReport)
                self.assertIsInstance(pending.retry, placement.RetryAt)
                deadline = pending.retry
                self.assertEqual(fixture.server.current_assignments[request.episode], key)
                self.assertNotIsInstance(fixture.connect(request).replies()[-1], wire.LocalPlacementGranted)
                fixture.clock.advance(10)
                _ = publish_candidate(fixture, request, placement.TemporaryExclusion('boot failed'))
                _ = fixture.connect(replace(report, request_id='boot-settled', path=placement.DispatchPathSettled())).replies()
                settled = fixture.server.assignments[key]
                self.assertIsInstance(settled.state, placement.ClosedAssignment)
                self.assertEqual(settled.retry, deadline)
                self.assertNotEqual(fixture.server.current_assignments.get(request.episode), key)
                fallback = fixture.connect(request).replies()[-1]
                self.assertIsInstance(fallback, wire.LocalPlacementGranted)
                assert isinstance(fallback, wire.LocalPlacementGranted)
                self.assertNotEqual(fallback.assignment.attempt, assignment.attempt)
                self.assertEqual(fallback.assignment.episode, assignment.episode)

    def test_dead_coordinator_releases_an_assignment_that_never_bound(self) -> None:
        with PlacementFixture() as fixture, mock.patch.object(service, 'HostEvidenceWorker', ManualEvidenceWorker):
            target = hana_target(fixture)
            controller = hana_box.HanaController(fixture.root / 'hana-state', fixture.root / 'account.conf',
                target.settings, hana_box.ProviderNotInstalled())
            request = placement_request(fixture)
            candidate = request.constraints.candidates[0]
            request = replace(request, constraints=replace(request.constraints, candidates=(replace(candidate,
                destination=replace(candidate.destination, provider='ssh', target=target.name)),)))
            with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                    mock.patch.object(hana_placement, 'controller_for', return_value=controller)):
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                fixture.end_owner(wire.Owner(assignment.owner.pid, assignment.owner.start_time))
                _ = fixture.run_pass()
                worker = fixture.server.host_targets[('ssh', target.name)]
                assert isinstance(worker, ManualEvidenceWorker)
                _ = worker.probe_next()
                _ = fixture.run_pass()
                saved = fixture.server.assignments[placement.attempt_key(assignment.episode, assignment.attempt)]
                self.assertIsInstance(saved.state, placement.ClosedAssignment)
                self.assertNotIn(request.episode, fixture.server.current_assignments)
                self.assertFalse(hana_execution.execution_path(controller.directory,
                    placement.assigned_claim_id(assignment.episode, assignment.attempt)).exists())

    def test_dead_coordinator_keeps_a_bound_assignment_with_missing_or_unreadable_journal(self) -> None:
        for journal in ('missing', 'unreadable'):
            with (self.subTest(journal=journal), PlacementFixture() as fixture,
                    mock.patch.object(service, 'HostEvidenceWorker', ManualEvidenceWorker)):
                target = hana_target(fixture)
                controller = hana_box.HanaController(fixture.root / 'hana-state', fixture.root / 'account.conf',
                    target.settings, hana_box.ProviderNotInstalled())
                request = placement_request(fixture)
                candidate = request.constraints.candidates[0]
                request = replace(request, constraints=replace(request.constraints, candidates=(replace(candidate,
                    destination=replace(candidate.destination, provider='ssh', target=target.name)),)))
                with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                        mock.patch.object(hana_placement, 'controller_for', return_value=controller)):
                    _ = publish_candidate(fixture, request)
                    assignment = assignment_for(fixture, request)
                    execution = placement.assigned_claim_id(assignment.episode, assignment.attempt)
                    controller.bind_execution(execution)
                    if journal == 'unreadable':
                        _ = hana_execution.execution_path(controller.directory, execution).write_text('{')
                    fixture.end_owner(wire.Owner(assignment.owner.pid, assignment.owner.start_time))
                    _ = fixture.run_pass()
                    worker = fixture.server.host_targets[('ssh', target.name)]
                    assert isinstance(worker, ManualEvidenceWorker)
                    _ = worker.probe_next()
                    _ = fixture.run_pass()
                    key = placement.attempt_key(assignment.episode, assignment.attempt)
                    self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                    self.assertEqual(fixture.server.current_assignments[request.episode], key)

    def test_controller_lock_does_not_block_a_queue_pass_or_release_reply(self) -> None:
        with PlacementFixture() as fixture:
            target = hana_target(fixture)
            controller = hana_box.HanaController(fixture.root / 'hana-state', fixture.root / 'account.conf',
                target.settings, hana_box.ProviderNotInstalled())
            request = placement_request(fixture)
            candidate = request.constraints.candidates[0]
            request = replace(request, constraints=replace(request.constraints, candidates=(replace(candidate,
                destination=replace(candidate.destination, provider='ssh', target=target.name)),)))
            locked, finish, reading = threading.Event(), threading.Event(), threading.Event()
            execution_bound = controller.execution_bound

            def hold_controller_lock() -> None:
                with control_lock(controller.directory):
                    locked.set()
                    if not finish.wait(TEST_HANG_GUARD_SECS):
                        raise AssertionError('The fixture did not release the controller lock')

            def read_bound(execution: str) -> bool:
                reading.set()
                return execution_bound(execution)

            with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                    mock.patch.object(hana_placement, 'controller_for', return_value=controller),
                    mock.patch.object(controller, 'execution_bound', side_effect=read_bound),
                    ThreadPoolExecutor(max_workers=2) as workers):
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                grant = only_admitted(fixture.connect(fixture.request()).replies())
                lock_owner = workers.submit(hold_controller_lock)
                try:
                    self.assertTrue(locked.wait(1))
                    fixture.proc.kill(wire.Owner(assignment.owner.pid, assignment.owner.start_time))
                    queue_pass = workers.submit(fixture.run_pass)
                    _ = queue_pass.result(timeout=1)
                    self.assertTrue(reading.wait(1))
                    began = time.monotonic()
                    reply = fixture.connect(wire.ReleaseRequest('release-with-controller-lock', grant.reservation, 0)).replies()
                    self.assertEqual(reply, [wire.Ok()])
                    self.assertLess(time.monotonic() - began, 1)
                    key = placement.attempt_key(assignment.episode, assignment.attempt)
                    self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                finally:
                    finish.set()
                _ = lock_owner.result(timeout=TEST_HANG_GUARD_SECS)
                worker = fixture.server.host_targets[('ssh', target.name)]
                worker.close()
                worker.thread.join(timeout=1)
                self.assertFalse(worker.thread.is_alive())

    def test_cancel_before_permission_use_settles_and_frees_capacity_with_a_live_coordinator(self) -> None:
        with PlacementFixture() as fixture, mock.patch.object(service, 'HostEvidenceWorker', ManualEvidenceWorker):
            target = hana_target(fixture)
            controller = hana_box.HanaController(fixture.root / 'hana-state', fixture.root / 'account.conf',
                target.settings, hana_box.ProviderNotInstalled())
            request = placement_request(fixture)
            candidate = request.constraints.candidates[0]
            request = replace(request, constraints=replace(request.constraints, candidates=(replace(candidate,
                destination=replace(candidate.destination, provider='ssh', target=target.name)),)))
            with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                    mock.patch.object(hana_placement, 'controller_for', return_value=controller)):
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                execution = placement.assigned_claim_id(assignment.episode, assignment.attempt)
                controller.bind_execution(execution)
                reference = placement.ProviderClaimReference('ssh', target.name, assignment.destination.scope,
                    execution, assignment.episode, assignment.attempt)
                _ = fixture.connect(wire.CancelPlacement('cancel-before-consume', request.episode, request.owner, 'cancelled')).replies()
                key = placement.attempt_key(assignment.episode, assignment.attempt)
                pending = fixture.server.assignments[key]
                self.assertIsInstance(pending.permission, placement.PermissionAvailable)
                self.assertIsInstance(pending.terminal_report, placement.TerminalAttemptReport)
                assert isinstance(pending.terminal_report, placement.TerminalAttemptReport)
                self.assertIsInstance(pending.terminal_report.path, placement.DispatchPathPending)
                self.assertIsInstance(pending.terminal_report.outcome, placement.AttemptCancelled)
                report = wire.ExecutionReport('cancel-settled', assignment, reference, placement.ExecutionNotStarted(),
                    placement.AttemptCancelled('cancelled'), placement.DispatchPathSettled())
                for invalid in (replace(report, claim=replace(reference, attempt='another-attempt')),
                        replace(report, outcome=placement.AttemptCompleted(0)),
                        replace(report, outcome=placement.AttemptCancelled('another-cancel'))):
                    _ = fixture.connect(invalid).replies()
                    self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                _ = fixture.connect(wire.ClaimFailure('refusal-after-cancel', assignment, 'disabled')).replies()
                self.assertEqual(fixture.server.assignments[key].terminal_report, pending.terminal_report)
                _ = fixture.connect(report).replies()
                self.assertIsInstance(fixture.server.assignments[key].state, placement.ClosedAssignment)
                self.assertNotIn(request.episode, fixture.server.current_assignments)
                self.assertEqual(fixture.proc.owner_start(request.owner.pid), service.LiveOwnerIdentity(request.owner.start_time))
                next_request = placement_request(fixture)
                next_request = replace(next_request, constraints=replace(next_request.constraints,
                    candidates=request.constraints.candidates))
                _ = publish_candidate(fixture, next_request)
                self.assertEqual(assignment_for(fixture, next_request).destination, assignment.destination)

    def test_unbound_refusal_reaches_fallback_after_worker_evidence_with_a_live_coordinator(self) -> None:
        with PlacementFixture() as fixture, mock.patch.object(service, 'HostEvidenceWorker', ManualEvidenceWorker):
            target = hana_target(fixture)
            controller = hana_box.HanaController(fixture.root / 'hana-state', fixture.root / 'account.conf',
                target.settings, hana_box.ProviderNotInstalled())
            request = placement_request(fixture)
            candidate = request.constraints.candidates[0]
            remote = replace(candidate, destination=replace(candidate.destination, provider='ssh', target=target.name))
            request = replace(request, prerequisite=placement.TokenNotRequired(),
                constraints=replace(request.constraints, candidates=(remote, local_candidate(fixture))))
            with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                    mock.patch.object(hana_placement, 'controller_for', return_value=controller)):
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                failure = wire.ClaimFailure('disabled-before-bind', assignment, 'disabled')
                _ = fixture.server.handle_request(failure)
                key = placement.attempt_key(assignment.episode, assignment.attempt)
                pending = fixture.server.assignments[key]
                self.assertIsInstance(pending.state, placement.CurrentAssignment)
                self.assertIsInstance(pending.terminal_report, placement.TerminalAttemptReport)
                fixture.clock.advance(10)
                _ = fixture.server.handle_request(failure)
                self.assertEqual(fixture.server.assignments[key].retry, pending.retry)
                _ = publish_candidate(fixture, request, placement.TemporaryExclusion('disabled'))
                _ = fixture.run_pass()
                worker = fixture.server.host_targets[('ssh', target.name)]
                assert isinstance(worker, ManualEvidenceWorker)
                _ = worker.probe_next()
                _ = fixture.run_pass()
                self.assertIsInstance(fixture.server.assignments[key].state, placement.ClosedAssignment)
                self.assertIsInstance(fixture.connect(request).replies()[-1], wire.LocalPlacementGranted)
                self.assertEqual(fixture.proc.owner_start(request.owner.pid), service.LiveOwnerIdentity(request.owner.start_time))

    def test_prelaunch_refusal_with_an_earlier_claim_settles_only_without_a_bound_dispatch(self) -> None:
        for bound in (False, True):
            with (self.subTest(bound=bound), PlacementFixture() as fixture,
                    mock.patch.object(service, 'HostEvidenceWorker', ManualEvidenceWorker)):
                target = hana_target(fixture)
                controller = hana_box.HanaController(fixture.root / 'hana-state', fixture.root / 'account.conf',
                    target.settings, hana_box.ProviderNotInstalled())
                request = placement_request(fixture)
                candidate = request.constraints.candidates[0]
                remote = replace(candidate, destination=replace(candidate.destination, provider='ssh', target=target.name))
                request = replace(request, prerequisite=placement.TokenNotRequired(),
                    constraints=replace(request.constraints, candidates=(remote, local_candidate(fixture))))
                with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                        mock.patch.object(hana_placement, 'controller_for', return_value=controller)):
                    _ = publish_candidate(fixture, request)
                    assignment = assignment_for(fixture, request)
                    if bound:
                        controller.bind_execution(placement.assigned_claim_id(assignment.episode, assignment.attempt))
                    retained = placement.ProviderClaimReference('ssh', target.name, assignment.destination.scope,
                        'retained-earlier-claim', assignment.episode, 'earlier-attempt')
                    _ = fixture.server.handle_request(wire.ClaimFailure('retained-before-bind', assignment, 'busy', retained))
                    key = placement.attempt_key(assignment.episode, assignment.attempt)
                    pending = fixture.server.assignments[key]
                    self.assertIsInstance(pending.state, placement.CurrentAssignment)
                    self.assertIsInstance(pending.permission, placement.PermissionAvailable)
                    self.assertEqual(pending.provider, retained)
                    self.assertIsInstance(pending.terminal_report, placement.TerminalAttemptReport)
                    assert isinstance(pending.terminal_report, placement.TerminalAttemptReport)
                    self.assertIsInstance(pending.terminal_report.path, placement.DispatchPathPending)
                    _ = publish_candidate(fixture, request, placement.TemporaryExclusion('busy'))
                    _ = fixture.run_pass()
                    worker = fixture.server.host_targets[('ssh', target.name)]
                    assert isinstance(worker, ManualEvidenceWorker)
                    _ = worker.probe_next()
                    _ = fixture.run_pass()
                    saved = fixture.server.assignments[key]
                    self.assertEqual(saved.provider, retained)
                    self.assertEqual(saved.retry, pending.retry)
                    self.assertEqual(fixture.proc.owner_start(request.owner.pid), service.LiveOwnerIdentity(request.owner.start_time))
                    if bound:
                        self.assertIsInstance(saved.state, placement.CurrentAssignment)
                        self.assertEqual(fixture.server.current_assignments[request.episode], key)
                    else:
                        self.assertIsInstance(saved.state, placement.ClosedAssignment)
                        self.assertNotEqual(fixture.server.current_assignments.get(request.episode), key)
                        self.assertIsInstance(fixture.connect(request).replies()[-1], wire.LocalPlacementGranted)

    def test_memory_eligibility_is_per_request_with_shared_host_evidence_in_both_orders(self) -> None:
        for order in ((1, 40), (40, 1)):
            with self.subTest(order=order), PlacementFixture() as fixture:
                target = hana_target(fixture)
                request = placement_request(fixture)
                candidate = request.constraints.candidates[0]
                request = replace(request, constraints=replace(request.constraints, candidates=(replace(candidate,
                    destination=replace(candidate.destination, provider='ssh', target=target.name)),)))
                with mock.patch.object(target_config, 'read_targets', return_value=(target,)):
                    shared = publish_candidate(fixture, request)
                    for gib in order:
                        sized = replace(request, episode=f'{request.episode}-{gib}', profile=replace(request.profile,
                            peak=records.ExpectedPeak(gib * 1024 ** 3, 'fallback', 0)))
                        self.assertEqual(fixture.server.host_evidence_key(sized,
                            request.constraints.candidates[0].destination), shared.key)
                        reply = fixture.connect(sized).replies()[-1]
                        if gib == 1:
                            self.assertIsInstance(reply, wire.Assignment)
                        else:
                            self.assertIsInstance(reply, wire.PlacementWait)
                            assert isinstance(reply, wire.PlacementWait)
                            self.assertEqual(reply.eligibility[0].eligibility, placement.TemporaryExclusion('memory'))
                            self.assertNotIn(sized.episode, fixture.server.current_assignments)
                        self.assertEqual(fixture.server.host_evidence(shared.key), shared)

    def test_hana_timeout_uses_the_aws_bound_without_ssh_keys(self) -> None:
        with PlacementFixture() as fixture:
            target = hana_target(fixture)
            request = placement_request(fixture)
            destination = replace(request.constraints.candidates[0].destination, provider='ssh', target=target.name)
            key = placement.HostEvidenceKey(destination, request.constraints.session, ('cargo', 'check'), 'configuration')
            with mock.patch.object(target_config, 'read_targets', return_value=(target,)):
                timeout = cast(Callable[[placement.HostEvidenceKey], float], getattr(fixture.server, '_host_probe_timeout'))
                self.assertEqual(timeout(key), 7)

    def test_hana_read_bypasses_ssh_policy_and_process_probes(self) -> None:
        with PlacementFixture() as fixture:
            settings = fixture.root / 'hana.conf'
            _ = settings.write_text('execution_transport=hana-control\nhost=fixture-hana\n')
            target = target_config.SshTarget('fixture-hana', 'linux', settings)
            request = placement_request(fixture)
            destination = replace(request.constraints.candidates[0].destination, provider='ssh', target=target.name)
            key = placement.HostEvidenceKey(destination, request.constraints.session, ('cargo', 'check'), 'configuration')
            fixture.server.host_requests[key] = request
            answer = placement.FreshHostEvidence(key, fixture.clock.now(), 'boot', placement.AllowedHost())
            with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                    mock.patch.object(hana_placement, 'read_evidence', return_value=answer) as read):
                self.assertEqual(fixture.server.read_provider_evidence(key), answer)
                read.assert_called_once_with(key, request)

    def test_worker_evidence_marks_the_snapshot_stale_on_the_loop_thread(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            _ = assignment_for(fixture, request)
            key = fixture.server.host_evidence_key(request, request.constraints.candidates[0].destination)
            worker = service.HostEvidenceWorker(key, fixture.server.host_probe, fixture.clock,
                fixture.server.reading_events, 1)
            self.addCleanup(worker.close)
            reader = service.HostEvidenceReader(key, worker, 1)
            fixture.server.host_readers[key] = reader
            updated = placement.FreshHostEvidence(key, fixture.clock.now(), 'new-boot', placement.TemporaryExclusion('starting'))
            reader.publish(updated)
            fixture.server.pass_stale = False
            refresh = cast(Callable[[], None], getattr(fixture.server, '_refresh_host_observations'))
            refresh()
            self.assertTrue(fixture.server.pass_stale)
            self.assertEqual(fixture.server.host_observations[key], updated)


class HanaGpuQueueTests(unittest.TestCase):
    def test_pinned_route_reports_settled_refusal_without_local_fallback(self) -> None:
        from contextlib import redirect_stderr
        from io import StringIO
        import os
        from pathlib import Path
        import subprocess

        from scripts.aws_target import gpu_size_trial, hana_placement_adapter
        from scripts.build_queue import admission_client
        from scripts.build_targets import build_targets
        from scripts.buildlog import treekey
        from scripts.copies import copies
        from scripts.direct.runner_result import UsableRunnerResult, read_result
        from scripts.direct.step_selection import Pinned
        from scripts.lib import process_start
        from scripts.mac_test import eligibility

        with PlacementFixture() as fixture:
            settings = fixture.root / 'cloud_gate.conf'
            repository = 'https://github.com/fixture/hana'
            _ = settings.write_text('execution_transport=hana-gpu\nhana_gpu_route_enabled=on\n'
                + f'hana_gpu_repository={repository}\nhana_gpu_instance_types=g6.2xlarge\n'
                + 'hana_gpu_selections=hana_mimesis_tools:shader_variants\n# range: 1..60\napi_timeout_s=30\n')
            _ = (fixture.root / 'build_targets.conf').write_text(
                'targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n'
                + 'pinned_targets=hana-gpu\nkind.hana-gpu=ssh\nplatform.hana-gpu=linux\n'
                + 'settings.hana-gpu=cloud_gate.conf\n')
            account = target_config.ssh_target_state_directory('aws')
            account.mkdir(parents=True)
            _ = (account / 'gate-deployment.json').write_text('{"gpu_instance_types":["g6.2xlarge"]}')
            pid = os.getpid()
            start = process_start.process_start(pid)
            assert isinstance(start, process_start.Started)
            fixture.proc.register(wire.Owner(pid, start.start))
            initial = placement_request(fixture)
            package = 'hana_mimesis_tools'
            command = ('cargo', 'nextest', 'run', '--no-fail-fast', '--workspace', '--test', 'shader_variants',
                       '-E', 'package(hana_mimesis_tools)')
            source = placement.SourceKeyKnown('source-a')
            reason = 'GPU result could not be collected'
            result_path = fixture.root / 'verify-result'

            with (mock.patch.object(eligibility, 'repository_identity',
                    return_value=eligibility.KnownRepository('hana', repository)),
                    mock.patch.object(treekey, 'tree_key', return_value=source.key),
                    mock.patch.object(copies, 'capture_source', return_value='captured')):
                constraints = build_targets.fixed_placement_constraints(
                    fixture.root, package, 'test', command, initial.constraints.session, source,
                    selection=Pinned('hana-gpu'))
                request = replace(initial, owner=placement.SubmittingOwner(fixture.server.host, pid, start.start),
                    run=wire.RunIdentity(pid, start.start), constraints=constraints, route=target_config.PinnedTarget('hana-gpu'))
                self.assertEqual(constraints.selection, placement.PinnedHost('hana-gpu'))
                candidate, = constraints.candidates
                self.assertEqual(candidate.eligibility, placement.AllowedHost())
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                self.assertEqual(assignment.destination.target, 'hana-gpu')
                key = fixture.server.host_evidence_key(request, assignment.destination)

                def refresh_evidence() -> None:
                    evidence = fixture.read_host_evidence(key)
                    self.assertIsInstance(evidence, placement.FreshHostEvidence)
                    assert isinstance(evidence, placement.FreshHostEvidence)
                    fixture.provider_evidence[key] = evidence
                    fixture.server.publish_host_evidence(evidence)

                def consume(message: wire.ConsumePermission) -> wire.FirstAuthorization:
                    refresh_evidence()
                    answer = fixture.connect(message).replies()[-1]
                    self.assertIsInstance(answer, wire.FirstAuthorization)
                    assert isinstance(answer, wire.FirstAuthorization)
                    return answer

                def decline(_repo: Path, _commit: str, _request: gpu_size_trial.HanaGpuNextestRequest, *,
                            launch: gpu_size_trial.AssignedHanaGpuLaunch) -> gpu_size_trial.HanaGpuNextestResult:
                    launch.before_service()
                    gpu_size_trial.confirm_assigned_gpu_service(launch.trial)
                    folder = gpu_size_trial.trial_root() / launch.trial
                    receipt = gpu_size_trial.cleanup_receipt(folder)
                    receipt.parent.mkdir(parents=True)
                    _ = receipt.write_text('{"trial":"' + launch.trial + '"}')
                    raise ValueError(reason)

                def report(message: wire.ExecutionReport) -> wire.DispatchStatus:
                    self.assertEqual(message.outcome, placement.AttemptDeclined(reason))
                    self.assertEqual(message.path, placement.DispatchPathSettled())
                    self.assertFalse(hana_placement_adapter.gpu_claim_pending('hana-gpu', message.claim.claim_id)
                                     if isinstance(message.claim, placement.ProviderClaimReference) else True)
                    refresh_evidence()
                    answer = fixture.connect(message).replies()[-1]
                    assert isinstance(answer, wire.DispatchStatus)
                    return answer

                with (mock.patch.object(admission_client, 'consume_permission', new=consume),
                      mock.patch.object(admission_client, 'report_execution', new=report),
                      mock.patch.object(gpu_size_trial, 'run_nextest', new=decline),
                      redirect_stderr(StringIO())):
                    status = hana_placement.run_gpu_assignment(assignment, fixture.root, package, command, result_path)
                self.assertEqual(status, gpu_size_trial.NEXTEST_INFRASTRUCTURE_EXIT)
                result = read_result(result_path, status, False)
                assert isinstance(result, UsableRunnerResult)
                self.assertEqual((result.word, result.reason), ('declined', reason))
                reply = fixture.connect(request).replies()[-1]
                assert isinstance(reply, wire.DispatchStatus)
                self.assertEqual(reply.state, placement.ClosedAssignment(reason))

            shell = Path(__file__).resolve().parents[1] / 'direct/verify.sh'
            script = shell.read_text()
            start_at = script.index('pinned_target_refuse()')
            stop_at = script.index('\nmac_only_refuse()', start_at)
            pinned_refusal = script[start_at:stop_at] + (
                'note_event() { :; }\nPINNED_TARGET=hana-gpu\npinned_target_refuse "$1"')
            refused = subprocess.run(['bash', '-c', pinned_refusal, 'verify.sh', result.reason],
                                     capture_output=True, text=True, timeout=5)
            self.assertEqual((refused.returncode, refused.stderr), (2, f'verify.sh: target hana-gpu refused: {reason}\n'))

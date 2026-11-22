"""Check inactive Hana placement with private journals and fake provider evidence."""
from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
from typing import cast, final, override
from unittest import mock

from scripts.build_queue import execution_identity
from scripts.aws_target import hana_box as box, hana_placement as hana, hana_placement_adapter as adapter, hana_spend, ledger, remote
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.controller_lock import HanaCommandAccess, hana_command_lock
from scripts.aws_target.hana_image_test_support import guest_digest_fixture
from scripts.aws_target.ledger_test_support import CostFixture, git_only, install_hana_spending_settings, text_setting
from scripts.aws_target.test_hana_box import FakeProvider, IDENTITY, record_lifecycle_image
from scripts.build_targets import build_targets, target_config
from scripts.build_queue import admission_protocol as wire, admission_records as records, placement
from scripts.buildlog import store
from scripts.lib import conf, process_start
from scripts.lib.atomic_file import write_text_atomic
from scripts.mac_test import claim_state, eligibility, mac_test
from scripts.build_hold.owning_session import OwningSession


REPOSITORY = Path(__file__).resolve().parents[2]
KEY = 'https://github.com/hanallc/hana'
PREVIEW_SECONDS = 2


class HanaPlacementTests(CostFixture):
    workload: Path = Path()
    target: target_config.SshTarget = target_config.SshTarget('fixture', 'linux', Path())
    clock: TestClock = TestClock()
    controller: box.HanaController
    provider: FakeProvider
    holder: placement.ClaimHolderIdentity = placement.ClaimHolderIdentity('', 0, '')
    assignment: placement.PlacementAssignment

    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.provider = FakeProvider(Path(), TestClock())
        self.controller = box.HanaController(Path(), Path(), Path(), self.provider)
        self.assignment = placement.PlacementAssignment('', '', placement.PlacementDestination(
            'ssh', 'fixture', 'fixture-hana', claim_state.RepositoryScope('hana', KEY, 1), 1),
            execution_identity.SourceKeyKnown('source'), placement.SubmittingOwner('', 0, ''), OwningSession('fixture-session'), 0)

    @override
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(guest_digest_fixture(self.root))
        _ = self.enterContext(mock.patch.object(subprocess, 'run', side_effect=git_only))
        self.workload = self.root / 'hana_aws.conf'
        install_hana_spending_settings(self.workload)
        _ = self.workload.write_text(self.workload.read_text()
            + f'execution_transport=hana-control\nrepository_identity={KEY}\nkinds=check,lint\n'
            + f'# whole: 1..5\npreview_max_age_s={PREVIEW_SECONDS}\n'
            + '# whole: 1..1\nmax_steps=1\n')
        self.clock = TestClock()
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(self.clock.now, (), 'private inventory', True))
        book.observe(ledger.InstanceObservation(self.clock.now, 'terminated', 'private account inventory'))
        self.provider = FakeProvider(self.state, self.clock)
        self.controller = box.HanaController(self.state, self.settings, self.workload, self.provider, clock=self.clock)
        self.controller.register(IDENTITY)
        _ = record_lifecycle_image(self.state, self.workload)
        self.target = target_config.SshTarget('fixture', 'linux', self.workload)
        targets = (self.target, target_config.LocalTarget('natedev', 'linux'))
        _ = self.enterContext(mock.patch.object(target_config, 'read_targets', return_value=targets))
        _ = self.enterContext(mock.patch.object(build_targets, 'read_targets', return_value=targets))
        _ = self.enterContext(mock.patch.object(eligibility, 'repository_identity',
            return_value=eligibility.KnownRepository('hana', KEY)))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {'BUILD_TARGETS_STATE_DIR': str(self.root / 'targets')})))
        _ = cast(object, self.enterContext(mock.patch.object(adapter, 'controller_for', return_value=self.controller)))
        start = process_start.process_start(os.getpid())
        assert isinstance(start, process_start.Started)
        self.holder = placement.ClaimHolderIdentity(store.host_name(), os.getpid(), start.start)
        self.assignment = replace(self.assignment, episode='fixture-episode', attempt='fixture-attempt',
            owner=placement.SubmittingOwner(self.holder.host, self.holder.pid, self.holder.start_time), since=float(self.clock.now))

    def claim(self) -> hana.HanaClaimResult:
        return hana.claim_assigned(self.assignment, self.holder, self.controller, worktree=str(self.root))

    def ready(self) -> hana.HanaClaimAdopted:
        self.assertIsInstance(self.claim(), hana.HanaClaimPending)
        for _step in range(4):
            self.clock.advance(1)
            _ = self.controller.tick()
        result = self.claim()
        assert isinstance(result, hana.HanaClaimAdopted), result
        return result

    def request(self, *, step: str = 'check', command: tuple[str, ...] = ('cargo', 'check')) -> wire.PlacementRequest:
        constraints = placement.PlacementConstraints(self.assignment.source_key, command, str(self.root), 'hana',
            self.assignment.session, (placement.PlacementCandidate(self.assignment.destination),), command_kind='check')
        profile = records.StepProfile('hana', step, str(self.root), records.ExpectedPeak(1024, 'fallback', 0))
        return wire.PlacementRequest(self.assignment.episode, self.assignment.owner, self.assignment.since, profile, constraints)

    def evidence(self, request: wire.PlacementRequest) -> placement.HostEvidence:
        key = placement.HostEvidenceKey(self.assignment.destination, request.constraints.session,
            request.constraints.command, 'fixture-config', str(self.root), 'hana', request.profile.step, command_kind='check')
        workload = conf.read(self.target.settings, load=conf.read_bytes)
        transport = target_config.read_execution_transport(self.target, configuration=workload)
        assert isinstance(transport, hana.HanaControlTransport), transport
        with mock.patch('time.time', self.clock), mock.patch('time.monotonic', self.clock):
            return hana.read_evidence(key, request, transport=transport, workload=workload)

    def eligibility(self) -> placement.HostEligibility:
        evidence = self.evidence(self.request())
        assert isinstance(evidence, placement.FreshHostEvidence)
        return evidence.eligibility

    def test_missing_image_digest_excludes_the_box_before_assignment(self) -> None:
        (self.state / 'hana/setup.json').unlink()
        self.assertEqual(self.eligibility(), placement.FixedExclusion(placement.HANA_IMAGE_STALE))
        self.assertEqual(self.provider.calls, [])

    @contextmanager
    def preview_calls(self) -> Generator[list[int]]:
        calls: list[int] = []
        preview = hana_spend.HanaSpending[hana_spend.StartPermitSettings].preview_start

        def count(spending: hana_spend.HanaSpending[hana_spend.StartPermitSettings], now: int, expires: int
                  ) -> hana_spend.StartAffordable | hana_spend.PermitRefused:
            calls.append(now)
            return preview(spending, now, expires)

        with mock.patch.object(hana_spend.HanaSpending, 'preview_start', count):
            yield calls

    def test_transport_selects_hana_without_ssh_settings(self) -> None:
        self.assertEqual(target_config.read_execution_transport(self.target), hana.HanaControlTransport(self.workload, 'fixture-hana'))
        text_setting(self.workload, 'execution_transport', 'ssh')
        self.assertEqual(target_config.read_execution_transport(self.target), hana.SshCommandTransport(self.workload))

    def test_unknown_transport_and_mac_hana_refuse(self) -> None:
        with self.assertRaises(conf.Unusable):
            _ = target_config.read_execution_transport(replace(self.target, platform='macos'))
        text_setting(self.workload, 'execution_transport', 'unknown')
        with self.assertRaises(conf.Unusable):
            _ = target_config.read_execution_transport(self.target)

    def test_disabled_and_wrong_repository_create_no_provider_calls(self) -> None:
        text_setting(self.workload, 'enabled', 'off')
        self.assertEqual(hana.fixed_policy(hana.HanaControlTransport(self.workload, 'fixture-hana'), str(self.root), 'check'),
            hana.HanaStepRefused('off'))
        text_setting(self.workload, 'enabled', 'on')
        with mock.patch.object(eligibility, 'repository_identity', return_value=eligibility.KnownRepository('hana', 'wrong-origin')):
            self.assertEqual(hana.fixed_policy(hana.HanaControlTransport(self.workload, 'fixture-hana'), str(self.root), 'check'),
                hana.HanaStepRefused('repo'))
        self.assertEqual(self.provider.calls, [])

    def test_only_judgments_pass_original_profile_and_command_checks(self) -> None:
        cases = [('mend', 'lint', ('cargo', 'clippy')), ('clippy', 'lint', ('cargo', 'clippy', '--fix')),
            ('clippy', 'lint', ('cargo', 'clippy', '--fix=true')), ('check', 'check', ('sh', '-c', 'cargo check')),
            ('check', 'check', ('cargo', 'check', '--config=x')), ('final', 'final', ('cargo', 'check')),
            ('nextest', 'test', ('cargo', 'nextest', 'run')), ('build', 'build', ('cargo', 'build'))]
        for step, kind, command in cases:
            with self.subTest(step=step, command=command):
                self.assertIsInstance(hana.classify_command(step, kind, command), hana.HanaStepRefused)
        self.assertIsInstance(hana.classify_command('check', 'check', ('cargo', '+nightly', 'check')), hana.HanaCheck)
        self.assertIsInstance(hana.classify_command('clippy', 'lint', ('cargo', 'clippy', '--', '-Dwarnings')), hana.HanaClippyJudgment)

    def test_fixed_candidates_preserve_order_and_use_one_slot_without_ssh(self) -> None:
        with mock.patch.object(eligibility, 'read_config', side_effect=AssertionError('No SSH settings')):
            constraints = build_targets.fixed_placement_constraints(self.root, 'hana', 'check', ('cargo', 'check'),
                self.assignment.session, 'source')
        self.assertEqual([item.destination.target for item in constraints.candidates], ['fixture', 'natedev'])
        self.assertEqual(constraints.candidates[0].destination, self.assignment.destination)
        self.assertIsInstance(constraints.candidates[0].eligibility, placement.AllowedHost)

    def test_disallowed_workloads_remain_excluded_when_pinned(self) -> None:
        from scripts.direct.step_selection import Pinned
        for kind in ('test', 'build', 'final'):
            with self.subTest(kind=kind):
                constraints = build_targets.fixed_placement_constraints(self.root, 'hana', cast(placement.CommandKind, kind),
                    ('cargo', 'check'), self.assignment.session, 'source', selection=Pinned('fixture'))
                self.assertIsInstance(constraints.candidates[0].eligibility, placement.FixedExclusion)
                self.assertEqual(constraints.candidates[0].eligibility, placement.FixedExclusion('command'))
                self.assertIsInstance(placement.retry_decision(constraints.candidates[0].destination, 'command', 0), placement.DoNotRetry)

    def test_claim_waits_for_a_controller_tick(self) -> None:
        entered, release = threading.Event(), threading.Event()
        read = self.controller._read  # pyright: ignore[reportPrivateUsage]

        def hold_tick() -> box.BoxRecord | box.NoDeployment:
            entered.set()
            if not release.wait(5):
                raise TimeoutError('The test did not release the controller tick')
            return read()

        with ThreadPoolExecutor(max_workers=2) as workers:
            # After the tick releases the lock, the claim can hold it during the provider call.
            with (mock.patch.object(self.controller, '_read', side_effect=hold_tick),
                    mock.patch.object(self.provider, 'check_intent')):
                tick = workers.submit(self.controller.tick)
                try:
                    self.assertTrue(entered.wait(5))
                    claim = workers.submit(self.controller.request, 'fixture-after-tick')
                    with self.assertRaises(TimeoutError):
                        _ = claim.result(timeout=0.1)
                finally:
                    release.set()
                self.assertIsInstance(claim.result(timeout=5), box.ClaimCommitted)
                self.assertEqual(tick.result(timeout=5), box.Lifecycle.IDLE_STOPPED)

    def test_setup_hold_excludes_assigned_claim_and_queue_evidence(self) -> None:
        execution = execution_identity.assigned_claim_id(self.assignment.episode, self.assignment.attempt)
        with ThreadPoolExecutor(max_workers=1) as worker:
            with hana_command_lock(self.state) as access:
                self.assertIs(access, HanaCommandAccess.ACQUIRED)
                claim = worker.submit(self.claim)
                self.assertIsInstance(claim.result(timeout=5), hana.HanaClaimResourcesHeld)
                inspect = worker.submit(self.controller.inspect)
                self.assertIsInstance(inspect.result(timeout=5), box.HanaResourcesHeld)
                evidence = worker.submit(self.evidence, self.request())
                snapshot = evidence.result(timeout=5)
                assert isinstance(snapshot, placement.FreshHostEvidence)
                self.assertEqual(snapshot.eligibility, placement.TemporaryExclusion('busy'))
                bound = worker.submit(self.controller.execution_bound, execution)
                self.assertFalse(bound.result(timeout=5))
                tick = worker.submit(self.controller.tick)
                self.assertIsInstance(tick.result(timeout=5), box.HanaResourcesHeld)
            self.assertIsInstance(worker.submit(self.claim).result(timeout=5), hana.HanaClaimPending)
        self.assertEqual(self.provider.calls, [])

    def test_provider_reply_settles_during_a_resource_hold(self) -> None:
        with ExitStack() as hold:
            def take_hold() -> None:
                self.assertIs(hold.enter_context(hana_command_lock(self.state)), HanaCommandAccess.ACQUIRED)

            self.provider.during_call = take_hold
            self.assertEqual(self.controller.tick(), box.Lifecycle.IDLE_STOPPED)
            snapshot = self.controller.inspect_for_settlement()
            assert isinstance(snapshot, box.HanaLifecycleEvidence)
            self.assertIsInstance(snapshot.operation, box.NoOperation)
            self.assertIsInstance(self.controller.tick(), box.HanaResourcesHeld)
            self.assertEqual(len(self.provider.calls), 1)

    def test_kernel_releases_the_setup_hold_when_setup_is_killed(self) -> None:
        child = """
import sys
from pathlib import Path
from scripts.aws_target.controller_lock import HanaCommandAccess, hana_command_lock
directory = Path(sys.argv[1])
with hana_command_lock(directory) as access:
    assert access is HanaCommandAccess.ACQUIRED
    print('held', flush=True)
    sys.stdin.read()
"""
        with (ThreadPoolExecutor(max_workers=1) as worker,
                subprocess.Popen([sys.executable, '-B', '-c', child, str(self.state)], cwd=REPOSITORY,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as process):
            try:
                assert process.stdout is not None
                self.assertTrue(select.select([process.stdout], [], [], 5)[0])
                self.assertEqual(process.stdout.readline(), 'held\n')
                inspect = worker.submit(self.controller.inspect)
                self.assertIsInstance(inspect.result(timeout=5), box.HanaResourcesHeld)
            finally:
                process.kill()
                _ = process.communicate(timeout=5)
        self.assertIsInstance(self.claim(), hana.HanaClaimPending)

    def test_claim_is_exact_and_busy_across_worktrees(self) -> None:
        first = self.claim()
        self.assertIsInstance(first, hana.HanaClaimPending)
        self.assertEqual(self.claim(), first)
        second = replace(self.assignment, episode='other', attempt='other')
        self.assertEqual(hana.claim_assigned(second, self.holder, self.controller), hana.HanaClaimRefused('Hana is busy'))
        self.assertEqual(self.provider.calls, [])

    def test_claim_rejects_wrong_slot_source_session_and_holder(self) -> None:
        variants = [replace(self.assignment, destination=replace(self.assignment.destination, slot=2)),
            replace(self.assignment, source_key=execution_identity.SourceKeyUnavailable('missing')),
            replace(self.assignment, destination=replace(self.assignment.destination, host='other'))]
        for assignment in variants:
            self.assertIsInstance(hana.claim_assigned(assignment, self.holder, self.controller), hana.HanaClaimRefused)
        self.assertIsInstance(hana.claim_assigned(self.assignment, replace(self.holder, start_time='wrong'), self.controller), hana.HanaClaimRefused)
        _ = self.claim()
        self.assertIsInstance(hana.claim_assigned(replace(self.assignment, session=OwningSession('other')), self.holder, self.controller), hana.HanaClaimRefused)

    def test_start_timeout_retains_exact_capacity(self) -> None:
        pending = self.claim()
        assert isinstance(pending, hana.HanaClaimPending)
        self.clock.advance(121)
        self.assertIsInstance(self.controller.release(pending.reference.claim_id), hana.HanaClaimReleaseRefused)
        self.assertEqual(self.claim(), pending)

    def test_placement_releases_only_a_dead_claim_after_lifecycle_abandonment(self) -> None:
        from scripts.aws_target import hana_execution

        pending = self.claim()
        assert isinstance(pending, hana.HanaClaimPending)
        other = replace(self.request(), episode='other')
        self.clock.advance(1)
        self.assertEqual(self.controller.tick(), box.Lifecycle.STARTING)
        with mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()):
            evidence = self.evidence(other)
            assert isinstance(evidence, placement.FreshHostEvidence)
            self.assertEqual(evidence.eligibility, placement.TemporaryExclusion('busy'))
            self.clock.advance(1)
            self.assertEqual(self.controller.tick(), box.Lifecycle.IDLE_STOPPED)
        evidence = self.evidence(other)
        assert isinstance(evidence, placement.FreshHostEvidence)
        self.assertEqual(evidence.eligibility, placement.TemporaryExclusion('busy'))
        with (hana_command_lock(self.state) as access,
                mock.patch.object(process_start, 'process_start', return_value=process_start.Gone())):
            self.assertIs(access, HanaCommandAccess.ACQUIRED)
            evidence = self.evidence(other)
            assert isinstance(evidence, placement.FreshHostEvidence)
            self.assertEqual(evidence.eligibility, placement.TemporaryExclusion('busy'))
            self.assertEqual(claim_state.read_claims(claim_state.state_paths('fixture')), ())
            self.assertIsInstance(hana_execution.read_claim_execution(self.state, pending.reference),
                                  execution_identity.ExecutionNotStarted)
        evidence = self.evidence(other)
        assert isinstance(evidence, placement.FreshHostEvidence)
        self.assertIsInstance(evidence.eligibility, placement.AllowedHost)
        self.assertEqual(claim_state.read_claims(claim_state.state_paths('fixture')), ())
        self.assertIsInstance(hana_execution.read_claim_execution(self.state, pending.reference),
                              execution_identity.ExecutionNotStarted)

    def test_failed_boot_never_recommits_demand_for_the_same_assignment(self) -> None:
        pending = self.claim()
        assert isinstance(pending, hana.HanaClaimPending)
        with (mock.patch.object(self.controller, 'claim_status', return_value=box.ClaimRefused('boot failed')),
                mock.patch.object(self.controller, 'request', side_effect=AssertionError('Do not restart the failed boot'))):
            self.assertEqual(self.claim(), hana.HanaClaimRefused('boot failed'))

    def test_placement_releases_a_dead_claim_after_command_admission_refusal(self) -> None:
        from scripts.aws_target import hana_execution as execution, test_hana_execution as fixtures

        adopted = self.ready()
        claim = adopted.reserved.claim.reference
        template = fixtures.HanaExecutionTests().request
        key = replace(template.key, claim_id=claim.claim_id, generation=adopted.generation)
        self.assertEqual((key.episode, key.attempt), (claim.episode, claim.attempt))
        source = self.assignment.source_key
        assert isinstance(source, execution_identity.SourceKeyKnown)
        request = replace(template, key=key, source_key=source)
        objects = fixtures.FakeExecutionObjects(request, terminated=True, lease_free=True, fail_fence=True)
        permit = self.provider.published[-1]
        _ = execution.dispatch_execution(self.state, request, adopted, objects,
            upload_source=lambda: None,
            consume=lambda selected: wire.FirstAuthorization(selected.assignment, selected.claim),
            current_permission=lambda: self.controller.permission(claim.claim_id, key.generation.boot_id, permit.generation),
            current_source_key=lambda: source)
        reason = 'The data mount and a fresh permit are required'
        observation = replace(objects.observe_execution(key), evidence=execution_identity.ExecutionEvidenceUnavailable(reason, self.clock.now))
        with mock.patch.object(objects, 'observe_execution', return_value=observation):
            _ = execution.observe_execution(self.state, request, objects)
        with mock.patch.object(hana, 'release_assigned', return_value=hana.HanaClaimReleaseRefused('The Hana claim is busy')) as release:
            settled = execution.settle_execution(self.state, request, objects,
                release_claim=lambda: hana.release_assigned(self.assignment, self.holder, self.controller), reason='lost',
                fence_window=execution.FenceAcknowledgementWindow(0, 1))
        self.assertIsInstance(settled, execution.HanaSettlementPending)
        self.assertTrue(execution.claim_dispatch_pending(self.state, claim))
        release.assert_called_once()
        other = replace(self.request(), episode='other')
        evidence = self.evidence(other)
        assert isinstance(evidence, placement.FreshHostEvidence)
        self.assertEqual(evidence.eligibility, placement.TemporaryExclusion('busy'))
        with mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()):
            for state in (box.Lifecycle.STOPPING, box.Lifecycle.STOPPING, box.Lifecycle.IDLE_STOPPED):
                self.clock.advance(1)
                self.assertEqual(self.controller.tick(), state)
            evidence = self.evidence(other)
        assert isinstance(evidence, placement.FreshHostEvidence)
        self.assertEqual(evidence.eligibility, placement.AllowedHost())
        self.assertEqual(claim_state.read_claims(claim_state.state_paths('fixture')), ())
        self.assertTrue(execution.claim_admission_refused(self.state, claim))

    def test_settled_attempt_cannot_commit_another_lifecycle_claim(self) -> None:
        from scripts.aws_target import hana_execution
        with (mock.patch.object(hana_execution, 'execution_command_settled', return_value=True),
                mock.patch.object(self.controller, 'request', side_effect=AssertionError('Do not reuse a settled attempt'))):
            self.assertIsInstance(self.claim(), hana.HanaClaimRefused)

    def test_refused_assignment_release_preserves_another_worktree_claim(self) -> None:
        _ = self.claim()
        second = replace(self.assignment, episode='other', attempt='other')
        from scripts.aws_target import hana_execution
        before = self.controller.path.read_bytes()
        with mock.patch.object(hana_execution, 'execution_command_settled', return_value=True):
            self.assertEqual(hana.release_assigned(second, self.holder, self.controller), hana.HanaClaimReleased())
        self.assertEqual(self.controller.path.read_bytes(), before)
        self.assertEqual(len(claim_state.read_claims(claim_state.state_paths('fixture'))), 1)

    def test_adoption_and_renewal_keep_the_boot_generation(self) -> None:
        adopted = self.ready()
        self.clock.advance(1)
        _ = self.controller.tick()
        repeated = self.claim()
        assert isinstance(repeated, hana.HanaClaimAdopted)
        self.assertEqual(repeated.generation, adopted.generation)
        self.assertEqual(repeated.reserved.claim.generation, hana.generation_text(adopted.generation))
        self.assertNotEqual(hana.generation_text(replace(adopted.generation, boot_id='another')), repeated.reserved.claim.generation)

    def test_evidence_never_calls_provider_or_ssh(self) -> None:
        adopted = self.ready()
        before = len(self.provider.calls)
        with mock.patch.object(eligibility, 'read_config', side_effect=AssertionError('No SSH settings')):
            evidence = self.evidence(self.request())
        assert isinstance(evidence, placement.FreshHostEvidence)
        self.assertIn(adopted.reserved.claim, evidence.claims)
        snapshot = self.controller.inspect()
        assert isinstance(snapshot, hana.HanaLifecycleEvidence)
        self.assertEqual(evidence.observed_at, snapshot.observed_at)
        self.assertEqual(len(self.provider.calls), before)

    def test_mend_and_unreadable_journals_refuse_evidence(self) -> None:
        mend = self.evidence(self.request(step='mend'))
        assert isinstance(mend, placement.FreshHostEvidence)
        self.assertEqual(mend.eligibility, placement.FixedExclusion('command'))
        _ = self.controller.path.write_text('{')
        self.assertIsInstance(self.evidence(self.request()), placement.HostEvidenceUnavailable)

    def test_spending_preview_matches_reservation_without_saving(self) -> None:
        book = ledger.CostLedger(self.state)
        spending = hana_spend.HanaSpending(book, self.settings, hana_spend.read_settings(conf.read(self.workload)))
        before = book.path.read_bytes()
        launches = book.launches
        history_reads: list[int] = []

        def count_launches() -> tuple[dict[str, object], ...]:
            history_reads.append(1)
            return launches()

        with mock.patch.object(book, 'launches', count_launches):
            preview = spending.preview_start(self.clock.now, self.clock.now + 1800)
        self.assertEqual(history_reads, [1])
        self.assertIsInstance(preview, hana_spend.StartAffordable)
        self.assertEqual(book.path.read_bytes(), before)
        self.assertIsInstance(spending.reserve_start(self.clock.now, self.clock.now + 1800), hana_spend.StartReserved)
        self.assertIsInstance(spending.preview_start(self.clock.now, self.clock.now + 1800), hana_spend.PermitRefused)

    def test_distinct_probes_share_one_preview_until_it_expires(self) -> None:
        first = self.clock.now
        with self.preview_calls() as calls:
            for index in range(10):
                evidence = self.evidence(replace(self.request(), episode=f'fixture-episode-{index}'))
                assert isinstance(evidence, placement.FreshHostEvidence)
                self.assertEqual(evidence.eligibility, placement.AllowedHost())
            self.clock.advance(PREVIEW_SECONDS - 1)
            self.assertEqual(self.eligibility(), placement.AllowedHost())
            self.assertEqual(calls, [first])
            self.clock.advance(1)
            self.assertEqual(self.eligibility(), placement.AllowedHost())
            self.assertEqual(calls, [first, first + PREVIEW_SECONDS])

    def test_ledger_change_refreshes_an_allowed_preview(self) -> None:
        book = ledger.CostLedger(self.state)
        settings = hana_spend.read_settings(conf.read(self.workload))
        spending = hana_spend.HanaSpending(book, self.settings, settings)
        with self.preview_calls() as calls:
            self.assertEqual(self.eligibility(), placement.AllowedHost())
            self.assertIsInstance(spending.reserve_start(self.clock.now, self.clock.now + settings.permit_max_seconds),
                                  hana_spend.StartReserved)
            self.assertEqual(self.eligibility(), placement.TemporaryExclusion('Confirm the previous stop before another start'))
            self.assertEqual(len(calls), 2)

    def test_refusal_is_shared_until_spending_configuration_changes(self) -> None:
        text_setting(self.workload, 'daily_budget_usd', '0.01')
        with self.preview_calls() as calls:
            for _probe in range(3):
                self.assertEqual(self.eligibility(), placement.TemporaryExclusion('hana_daily_budget_usd'))
            self.assertEqual(len(calls), 1)
            text_setting(self.workload, 'daily_budget_usd', '6')
            self.assertEqual(self.eligibility(), placement.AllowedHost())
            self.costs({'daily_limit_usd': '0.01', 'limit_reserve_usd': '0'})
            self.assertEqual(self.eligibility(), placement.TemporaryExclusion('daily_limit'))
            self.assertEqual(len(calls), 3)

    def test_workload_edit_after_routing_keeps_the_probe_budget_and_expires_its_preview(self) -> None:
        inspect = self.controller.inspect

        def inspect_after_edit() -> box.HanaInspection:
            text_setting(self.workload, 'daily_budget_usd', '0.01')
            return inspect()

        with self.preview_calls() as calls:
            with mock.patch.object(self.controller, 'inspect', side_effect=inspect_after_edit):
                self.assertEqual(self.eligibility(), placement.AllowedHost())
            self.assertEqual(self.eligibility(), placement.TemporaryExclusion('hana_daily_budget_usd'))
            self.assertEqual(calls, [self.clock.now] * 2)

    def test_preview_does_not_cache_a_budget_recovered_from_git(self) -> None:
        lines = self.workload.read_text().splitlines(keepends=True)
        _ = self.workload.write_text(''.join(line for line in lines if not line.startswith('daily_budget_usd=')))
        recovered = [iter(('daily_budget_usd=0.01\n',)), iter(('daily_budget_usd=6\n',))]
        with self.preview_calls() as calls, conf.report_key_changes(), redirect_stderr(StringIO()) as warnings:
            with mock.patch.object(conf, '_committed_files', side_effect=recovered) as history:
                self.assertEqual(self.eligibility(), placement.TemporaryExclusion('hana_daily_budget_usd'))
                self.assertEqual(self.eligibility(), placement.AllowedHost())
        self.assertEqual(history.call_args_list, [mock.call(self.workload)] * 2)
        self.assertEqual(calls, [self.clock.now] * 2)
        self.assertEqual(warnings.getvalue().count("daily_budget_usd='<missing>'"), 1)

    def test_history_lifecycle_and_setup_writes_refresh_the_preview(self) -> None:
        history = ledger.CostLedger(self.state).launch_history_path
        with self.preview_calls() as calls:
            self.assertEqual(self.eligibility(), placement.AllowedHost())
            _ = history.write_text('{}\n')
            self.assertEqual(self.eligibility(), placement.AllowedHost())
            write_text_atomic(self.controller.path, self.controller.path.read_text())
            self.assertEqual(self.eligibility(), placement.AllowedHost())
            setup = self.controller.path.with_name('setup.json')
            record = remote.read_record(setup)
            record['instance_type'] = 'c7a.4xlarge'
            write_text_atomic(setup, json.dumps(record))
            self.assertEqual(self.eligibility(), placement.AllowedHost())
            self.assertEqual(len(calls), 4)

    def test_bill_creation_and_update_refresh_the_preview(self) -> None:
        end = self.clock.now // 86400 * 86400
        day = datetime.fromtimestamp(end - 86400, UTC).date().isoformat()
        bill = ledger.ChargeRead(day, self.clock.now, end - 86400, end, Decimal(0), Decimal(0), Decimal(0))
        directory = self.state / 'charges'
        directory.mkdir()
        path = directory / f'{day}.json'
        with self.preview_calls() as calls:
            self.assertEqual(self.eligibility(), placement.AllowedHost())
            _ = path.write_text(json.dumps(ledger.charge_record(bill)))
            self.assertEqual(self.eligibility(), placement.AllowedHost())
            _ = path.write_text(json.dumps(ledger.charge_record(replace(bill, actual_usd=Decimal(1)))))
            self.assertEqual(self.eligibility(), placement.AllowedHost())
            self.assertEqual(len(calls), 3)

    def test_provider_start_rechecks_spending_after_an_allowed_preview(self) -> None:
        self.assertEqual(self.eligibility(), placement.AllowedHost())
        self.assertIsInstance(self.claim(), hana.HanaClaimPending)
        check_start = hana_spend.HanaSpending[hana_spend.PermitSettings].check_start
        checked: list[hana_spend.StartReserved | hana_spend.PermitRefused] = []

        def check(spending: hana_spend.HanaSpending[hana_spend.PermitSettings], now: int, generation: int
                  ) -> hana_spend.StartReserved | hana_spend.PermitRefused:
            self.assertEqual(spending.book.read_snapshot().hana_compute[0].generation, generation)
            self.costs({'daily_limit_usd': '0.01', 'limit_reserve_usd': '0'})
            result = check_start(spending, now, generation)
            checked.append(result)
            return result

        with mock.patch.object(hana_spend.HanaSpending, 'check_start', check):
            _ = self.controller.tick()
        self.assertEqual(checked, [hana_spend.PermitRefused('daily_limit')])
        self.assertEqual(self.provider.calls, [])

    def test_inspection_keeps_unreadable_and_unprovisioned_distinct(self) -> None:
        self.controller.path.unlink()
        self.assertIsInstance(self.controller.inspect(), hana.HanaUnprovisioned)
        _ = self.controller.path.write_text('{}')
        self.assertIsInstance(self.controller.inspect(), hana.HanaLifecycleUnavailable)

    def test_release_requires_durable_fence_and_exact_holder(self) -> None:
        adopted = self.ready()
        self.assertEqual(hana.release_assigned(self.assignment, self.holder, self.controller),
                         hana.HanaClaimReleaseRefused('The Hana command settlement is unconfirmed'))
        from scripts.aws_target import hana_execution
        with mock.patch.object(hana_execution, 'execution_command_settled', return_value=True):
            self.assertEqual(hana.release_assigned(self.assignment, replace(self.holder, start_time='wrong'), self.controller),
                             hana.HanaClaimReleaseRefused('The local Hana claim differs from the assignment or holder'))
            step = self.provider.step

            def during_observe(identity: box.DeploymentIdentity, start: box.DemandStart | box.NoDemandStart,
                               operation: box.PendingOperation) -> box.ProviderReply:
                self.assertEqual(operation.action, box.Action.OBSERVE)
                self.assertEqual(hana.release_assigned(self.assignment, self.holder, self.controller),
                                 hana.HanaClaimReleaseRefused('The Hana lifecycle operation observe is pending'))
                return step(identity, start, operation)

            with mock.patch.object(self.provider, 'step', side_effect=during_observe) as observed:
                _ = self.controller.tick()
            observed.assert_called_once()
            self.assertEqual(hana.release_assigned(self.assignment, self.holder, self.controller), hana.HanaClaimReleased())
            self.assertEqual(hana.release_assigned(self.assignment, self.holder, self.controller), hana.HanaClaimReleased())
        self.assertEqual(claim_state.read_claims(claim_state.state_paths('fixture')), ())
        self.assertEqual(adopted.reserved.claim.reference.claim_id, execution_identity.assigned_claim_id('fixture-episode', 'fixture-attempt'))

    def test_generation_import_is_guest_safe(self) -> None:
        code = "import sys; from scripts.aws_target.hana_placement import HanaHostGeneration; assert 'scripts.aws_target.hana_provider' not in sys.modules"
        result = subprocess.CompletedProcess[str]([], 0)
        # Use the real interpreter because the fixture refuses non-Git commands.
        from scripts.aws_target.ledger_test_support import REAL_RUN
        result = REAL_RUN([sys.executable, '-E', '-c', code], cwd=REPOSITORY, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_an_exact_current_assignment_establishes_a_remote_waiter(self) -> None:
        from scripts.build_hold import build_hold, hold_enforcer
        from scripts.build_queue import admission_service
        _ = self.ready()
        key = execution_identity.attempt_key(self.assignment.episode, self.assignment.attempt)
        saved = records.SavedPlacementAssignment(self.assignment)
        state = admission_service.ServiceState({}, {}, {}, records.ReadyToStart(),
            assignments={key: saved}, current_assignments={self.assignment.episode: key})
        with (mock.patch.object(admission_service, 'read_state', return_value=state),
                mock.patch.object(hold_enforcer, 'remote_step_callers', return_value=frozenset()),
                mock.patch.object(build_hold, 'process_start_time', return_value=self.holder.start_time)):
            claims = hold_enforcer.claimed_processes(self.root / 'proc')
            assert isinstance(claims, build_hold.LiveClaims)
            self.assertEqual(claims.remote_waiters, frozenset({build_hold.ProcessIdentity(self.holder.pid, self.holder.start_time)}))
            state.assignments[key] = replace(saved, state=placement.ClosedAssignment('completed'))
            claims = hold_enforcer.claimed_processes(self.root / 'proc')
            assert isinstance(claims, build_hold.LiveClaims)
            self.assertEqual(claims.remote_waiters, frozenset())
            self.assertEqual(claims.identities, frozenset({build_hold.ProcessIdentity(self.holder.pid, self.holder.start_time)}))


@final
class HanaGpuPlacementTests(CostFixture):
    package = 'hana_mimesis_tools'
    command = ('cargo', 'nextest', 'run', '--profile', 'ci', '--no-fail-fast', '--workspace',
               '--test', 'shader_variants', '-E', 'package(hana_mimesis_tools)')
    assignment: placement.PlacementAssignment
    gpu_settings = Path()
    account_dir = Path()
    trials = Path()
    grants = Path()

    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.assignment = placement.PlacementAssignment('', '', placement.PlacementDestination(
            'ssh', 'hana-gpu', 'hana-gpu', claim_state.WHOLE_MAC), execution_identity.SourceKeyUnavailable('fixture'),
            placement.SubmittingOwner('', 0, ''), OwningSession('fixture-session'), 0)

    @override
    def setUp(self) -> None:
        super().setUp()
        self.gpu_settings = self.root / 'cloud_gate.conf'
        self.account_dir = self.root / 'targets/aws'
        self.trials = self.root / 'trials'
        gpu_target = target_config.SshTarget('hana-gpu', 'linux', self.gpu_settings)
        _ = self.gpu_settings.write_text(
            'execution_transport=hana-gpu\nhana_gpu_route_enabled=on\n'
            + f'hana_gpu_repository={KEY}\nhana_gpu_instance_types=g4dn.2xlarge g6.2xlarge\n'
            + 'hana_gpu_selections=hana_mimesis_tools:shader_variants\n# range: 1..60\napi_timeout_s=30\n')
        self.account_dir.mkdir(parents=True)
        self.grants = self.account_dir / 'gate-deployment.json'
        _ = self.grants.write_text(json.dumps({'gpu_instance_types': ['g4dn.2xlarge', 'g6.2xlarge']}))
        _ = (self.root / 'build_targets.conf').write_text(
            'targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n'
            + 'pinned_targets=hana-gpu\nkind.hana-gpu=ssh\nplatform.hana-gpu=linux\n'
            + f'settings.hana-gpu={gpu_target.settings}\n')
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {'CONF_DIR': str(self.root)})))
        _ = self.enterContext(mock.patch.object(eligibility, 'repository_identity',
            return_value=eligibility.KnownRepository('hana', KEY)))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {'BUILD_TARGETS_STATE_DIR': str(self.account_dir.parent)})))
        pid = os.getpid()
        process = process_start.process_start(pid)
        assert isinstance(process, process_start.Started)
        self.assignment = placement.PlacementAssignment('episode', 'attempt',
            placement.PlacementDestination('ssh', 'hana-gpu', 'hana-gpu', claim_state.RepositoryScope('hana', KEY, 1), 1),
            execution_identity.SourceKeyKnown('source'), placement.SubmittingOwner(store.host_name(), pid, process.start),
            OwningSession('fixture-session'), 0)

    def candidate(self, *, command: tuple[str, ...] = command) -> placement.PlacementCandidate:
        from scripts.direct.step_selection import Pinned

        constraints = build_targets.fixed_placement_constraints(
            self.root, self.package, 'test', command, self.assignment.session, self.assignment.source_key,
            selection=Pinned('hana-gpu'))
        return next(candidate for candidate in constraints.candidates if candidate.destination.target == 'hana-gpu')

    def test_admits_only_the_pinned_configured_test_in_one_slot(self) -> None:
        candidate = self.candidate()
        self.assertEqual(candidate.eligibility, placement.AllowedHost())
        self.assertEqual(candidate.destination, self.assignment.destination)
        routed = build_targets.fixed_placement_constraints(
            self.root, self.package, 'test', self.command, self.assignment.session, self.assignment.source_key)
        self.assertEqual([item.destination.target for item in routed.candidates], ['natedev'])
        for command in (self.command[:-3] + ('another_test', *self.command[-2:]), (*self.command, '--run-ignored', 'only')):
            self.assertEqual(self.candidate(command=command).eligibility,
                             placement.FixedExclusion(adapter.GPU_SELECTION_REQUIRED))

    def test_refuses_when_setup_is_off_or_missing_a_type(self) -> None:
        grants = self.grants.read_text()
        _ = self.grants.write_text(json.dumps({'gpu_instance_types': ['g6.2xlarge']}))
        self.assertEqual(self.candidate().eligibility, placement.FixedExclusion(adapter.GPU_SETUP_REQUIRED))
        _ = self.grants.write_text(grants)
        _ = self.gpu_settings.write_text(self.gpu_settings.read_text().replace('enabled=on', 'enabled=off'))
        self.assertEqual(self.candidate().eligibility, placement.FixedExclusion(adapter.GPU_SETUP_REQUIRED))

    def run_fake_gpu(self, code: int | str, *, confirm_cleanup: bool = True, refuse_start: bool = False,
                     report_unavailable: bool = False, refuse_release: bool = False
                     ) -> tuple[int, str, str, wire.ExecutionReport]:
        from contextlib import redirect_stdout
        from scripts.aws_target import gpu_size_trial
        from scripts.build_queue import admission_client
        from scripts.buildlog import treekey
        from scripts.copies import copies

        claim_id = execution_identity.assigned_claim_id(self.assignment.episode, self.assignment.attempt)
        reference = placement.ProviderClaimReference('ssh', 'hana-gpu', self.assignment.destination.scope,
                                                      claim_id, self.assignment.episode, self.assignment.attempt)
        reports: list[wire.ExecutionReport] = []
        result = self.root / 'gpu-result'
        stdout, stderr = StringIO(), StringIO()
        real_run = gpu_size_trial.run_nextest
        real_unlink = Path.unlink
        claim_path = mac_test.claim_path(claim_state.state_paths('hana-gpu'), self.assignment.destination.scope)

        def unlink(path: Path, missing_ok: bool = False) -> None:
            if refuse_release and path == claim_path:
                raise PermissionError('The fixture cannot remove the GPU claim')
            real_unlink(path, missing_ok=missing_ok)

        def run(repo: Path, commit: str, request: gpu_size_trial.HanaGpuNextestRequest, *,
                launch: gpu_size_trial.AssignedHanaGpuLaunch) -> gpu_size_trial.HanaGpuNextestResult:
            self.assertEqual((repo, commit, request.arguments, launch.trial),
                             (self.root, 'captured', self.command[3:], claim_id))
            if refuse_start:
                def refuse(_folder: Path, _directory: Path, _settings: Path,
                           _execute: Callable[[Sequence[str]], int], *, assigned: bool) -> int:
                    self.assertTrue(assigned)
                    launch.before_service()
                    self.assertFalse(gpu_size_trial.assigned_gpu_cleanup_pending(claim_id))
                    return 1

                with (mock.patch.object(gpu_size_trial, 'prepare_locked'),
                      mock.patch.object(gpu_size_trial, 'start_service', new=refuse)):
                    return real_run(repo, commit, request, launch=launch)
            launch.before_service()
            self.assertFalse(gpu_size_trial.assigned_gpu_cleanup_pending(claim_id))
            gpu_size_trial.confirm_assigned_gpu_service(claim_id)
            self.assertTrue(gpu_size_trial.assigned_gpu_cleanup_pending(claim_id))
            if confirm_cleanup:
                receipt = gpu_size_trial.cleanup_receipt(self.trials / claim_id)
                receipt.parent.mkdir(parents=True)
                _ = receipt.write_text(json.dumps({'trial': claim_id}))
            if isinstance(code, str):
                raise ValueError(code)
            return gpu_size_trial.HanaGpuNextestResult('cloud-attempt', commit, 'tree', code, 'output\n', 'diagnostics\n')

        def report(request: wire.ExecutionReport) -> wire.DispatchStatus:
            reports.append(request)
            self.assertIn('outcome=', result.read_text())
            if report_unavailable:
                self.assertEqual(stdout.getvalue(), 'output\n')
                self.assertEqual(stderr.getvalue(), 'diagnostics\n')
                raise wire.AdmissionUnavailable('fixture queue outage')
            state = (placement.ClosedAssignment('completed')
                     if isinstance(request.outcome, placement.AttemptCompleted)
                     and isinstance(request.path, placement.DispatchPathSettled)
                     else placement.CurrentAssignment())
            return wire.DispatchStatus(self.assignment, state, placement.PermissionConsumed(0))

        with (mock.patch.object(treekey, 'tree_key', return_value='source'),
                mock.patch.object(copies, 'capture_source', return_value='captured'),
                mock.patch.object(gpu_size_trial, 'run_nextest', new=run),
                mock.patch.object(gpu_size_trial, 'trial_root', return_value=self.trials),
                mock.patch.object(admission_client, 'consume_permission', return_value=wire.FirstAuthorization(
                    self.assignment, reference)),
                mock.patch.object(admission_client, 'report_execution', new=report),
                mock.patch.object(Path, 'unlink', new=unlink),
                redirect_stdout(stdout), redirect_stderr(stderr)):
            status = adapter.run_gpu_assignment(self.assignment, self.root, self.package, self.command, result)
        self.assertIn('outcome=', result.read_text())
        self.assertEqual(adapter.gpu_claim_pending('hana-gpu', claim_id), refuse_release or not confirm_cleanup)
        self.assertEqual(len(reports), 1)
        return status, stdout.getvalue(), stderr.getvalue(), reports[0]

    def test_returns_the_pass_or_fail_status_and_output(self) -> None:
        from scripts.aws_target import gpu_size_trial

        for code in (0, 100):
            with self.subTest(code=code):
                status, stdout, stderr, report = self.run_fake_gpu(code)
                self.assertEqual((status, stdout, stderr), (code, 'output\n', 'diagnostics\n'))
                self.assertEqual(report.outcome, placement.AttemptCompleted(code))
                self.assertEqual(report.path, placement.DispatchPathSettled())
                with mock.patch.object(gpu_size_trial, 'trial_root', return_value=self.trials):
                    folder = self.trials / execution_identity.assigned_claim_id(self.assignment.episode, self.assignment.attempt)
                    receipt = gpu_size_trial.cleanup_receipt(folder)
                    receipt.unlink()
                    receipt.parent.rmdir()

    def test_reports_infrastructure_refusal_after_cleanup(self) -> None:
        status, stdout, stderr, report = self.run_fake_gpu('Spot capacity unavailable')
        self.assertEqual((status, stdout), (125, ''))
        self.assertIn('Spot capacity unavailable', stderr)
        self.assertEqual(report.outcome, placement.AttemptDeclined('Spot capacity unavailable'))
        self.assertEqual(report.path, placement.DispatchPathSettled())

    def test_retains_capacity_when_cleanup_has_no_confirmation(self) -> None:
        status, stdout, _, report = self.run_fake_gpu('The GPU service ended', confirm_cleanup=False)
        self.assertEqual((status, stdout), (125, ''))
        self.assertEqual(report.outcome, placement.AttemptOutcomeUnavailable('The GPU service ended'))
        self.assertEqual(report.path, placement.DispatchPathPending('The GPU service ended'))

    def test_reconciled_termination_releases_the_retained_slot(self) -> None:
        from scripts.aws_target import gpu_size_trial
        from scripts.aws_target.controller_lock import control_lock

        _ = self.run_fake_gpu('Unconfirmed termination', confirm_cleanup=False)
        claim = execution_identity.assigned_claim_id(self.assignment.episode, self.assignment.attempt)
        destination = self.assignment.destination
        transport = target_config.read_execution_transport(target_config.configured_ssh_target(destination.target))
        from scripts.build_targets.execution_transport import HanaGpuNextestTransport
        assert isinstance(transport, HanaGpuNextestTransport)
        key = placement.HostEvidenceKey(destination, self.assignment.session, self.command, 'fixture',
            package=self.package, command_kind='test')
        constraints = placement.PlacementConstraints(self.assignment.source_key, self.command, str(self.root),
            self.package, self.assignment.session, (self.candidate(),), placement.PinnedHost(destination.target),
            command_kind='test')
        profile = records.StepProfile(self.package, 'nextest', 'source', records.ExpectedPeak(1024, 'fallback', 0))
        request = wire.PlacementRequest(self.assignment.episode, self.assignment.owner, 0, profile, constraints)
        book = ledger.CostLedger(self.account_dir)
        worker_lock = self.account_dir / 'gpu-size-trial'
        booking = ledger.GateReservation('cloud-attempt', 10, 20, Decimal(1), Decimal(0),
            state=ledger.GateRunning('i-fixture'), trial=ledger.GateTrialBudget(claim, Decimal(2)),
            mode='gpu-size-trial', instance_type='g6.2xlarge', region='us-east-1')
        closed = replace(booking, state=ledger.GateTerminated(25, 'i-fixture'))
        other_trial = replace(closed, attempt='unrelated', trial=ledger.GateTrialBudget('another-trial', Decimal(2)))

        with (mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()),
                mock.patch.object(gpu_size_trial, 'trial_root', return_value=self.trials)):
            with control_lock(worker_lock), book.transaction() as state:
                state.gate_reservations = [other_trial]
            self.assertTrue(adapter.read_gpu_evidence(transport, key, request).cleanup_claims)
            with book.transaction() as state:
                state.gate_reservations.append(booking)
            self.assertTrue(adapter.read_gpu_evidence(transport, key, request).cleanup_claims)
            with control_lock(worker_lock):
                with book.transaction() as state:
                    state.gate_reservations = [other_trial, closed]
                self.assertTrue(adapter.read_gpu_evidence(transport, key, request).cleanup_claims)
            evidence = adapter.read_gpu_evidence(transport, key, request)
            self.assertFalse(evidence.cleanup_claims)
            self.assertEqual(evidence.eligibility, placement.AllowedHost())
            self.assertFalse(adapter.gpu_claim_pending(destination.target, claim))

    def test_refused_service_start_releases_its_slot(self) -> None:
        from scripts.direct.runner_result import UsableRunnerResult, read_result

        for refuse_release, expected in (
            (False, placement.DispatchPathSettled()),
            (True, placement.DispatchPathPending('The Hana GPU claim release remains pending')),
        ):
            with self.subTest(refuse_release=refuse_release):
                self.assignment = replace(self.assignment, attempt=f'refusal-{refuse_release}')
                status, stdout, stderr, report = self.run_fake_gpu(
                    'unused', refuse_start=True, refuse_release=refuse_release)
                self.assertEqual((status, stdout), (75, ''))
                reason = 'The Hana GPU service did not start (status 1)'
                self.assertIn(reason, stderr)
                self.assertEqual(report.outcome, execution_identity.HanaCommandAdmissionRefused(reason))
                self.assertEqual(report.evidence, execution_identity.ExecutionNotStarted())
                self.assertEqual(report.path, expected)
                result = read_result(self.root / 'gpu-result', status, False)
                assert isinstance(result, UsableRunnerResult)
                self.assertEqual((result.word, result.reason), ('hana_command_admission_refused', reason))

    def test_pending_claim_release_preserves_the_test_verdict_and_output(self) -> None:
        status, stdout, stderr, report = self.run_fake_gpu(100, refuse_release=True)
        self.assertEqual((status, stdout, stderr), (100, 'output\n', 'diagnostics\n'))
        self.assertEqual(report.outcome, placement.AttemptCompleted(100))
        self.assertEqual(report.path, placement.DispatchPathPending('The Hana GPU claim release remains pending'))

    def test_queue_outage_preserves_the_test_verdict_and_output(self) -> None:
        status, stdout, stderr, report = self.run_fake_gpu(100, report_unavailable=True)
        self.assertEqual((status, stdout), (100, 'output\n'))
        self.assertEqual(stderr, 'diagnostics\nHana GPU queue report failed: fixture queue outage\n')
        self.assertEqual(report.outcome, placement.AttemptCompleted(100))

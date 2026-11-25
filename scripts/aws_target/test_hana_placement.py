"""Check inactive Hana placement with private journals and fake provider evidence."""
from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
from scripts.aws_target.ledger_test_support import CostFixture, git_only, install_hana_spending_settings, text_setting
from scripts.aws_target.test_hana_box import FakeProvider, IDENTITY, record_lifecycle_image
from scripts.build_targets import build_targets, target_config
from scripts.build_queue import admission_protocol as wire, admission_records as records, placement
from scripts.buildlog import store
from scripts.lib import conf, process_start
from scripts.lib.atomic_file import write_text_atomic
from scripts.mac_test import claim_state, eligibility, mac_test
from scripts.build_hold.owning_session import OwningSession
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE


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
            'ssh', 'fixture', 'fixture-hana', claim_state.RepositoryScope('hana', KEY), 1),
            execution_identity.SourceKeyKnown('source'), placement.SubmittingOwner('', 0, ''), OwningSession('fixture-session'), 0)

    @override
    def setUp(self) -> None:
        super().setUp()
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

    def test_refused_route_precedes_controller_and_target_state(self) -> None:
        """Hana read_evidence row 3."""
        paths = claim_state.state_paths(self.target.name)
        text_setting(self.workload, 'enabled', 'off')
        with (mock.patch.object(adapter, 'controller_for', wraps=adapter.controller_for) as controller,
              mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock):
            self.assertEqual(self.eligibility(), placement.FixedExclusion('off'))
        controller.assert_not_called()
        lock.assert_not_called()
        self.assertFalse(paths.directory.exists())
        self.assertEqual(self.provider.calls, [])

    def test_claim_directory_and_lock_exist_before_the_lifecycle_read(self) -> None:
        """Hana read_evidence row 7."""
        paths = claim_state.state_paths(self.target.name)
        self.assertFalse(paths.directory.exists())
        inspect = self.controller.inspect

        def inspect_after_open() -> box.HanaInspection:
            self.assertTrue(paths.lock.is_file())
            return inspect()

        with mock.patch.object(self.controller, 'inspect', side_effect=inspect_after_open) as lifecycle:
            self.assertEqual(self.eligibility(), placement.AllowedHost())
        lifecycle.assert_called_once()

    def test_lifecycle_inspection_runs_under_the_target_lock(self) -> None:
        """Hana read_evidence row 8."""
        paths = claim_state.state_paths(self.target.name)
        inspect = self.controller.inspect

        def inspect_under_lock() -> box.HanaInspection:
            with self.assertRaises(BlockingIOError), claim_state.locked(paths.lock, wait=False):
                self.fail('The target lock is free')
            return inspect()

        with mock.patch.object(self.controller, 'inspect', side_effect=inspect_under_lock) as lifecycle:
            self.assertEqual(self.eligibility(), placement.AllowedHost())
        lifecycle.assert_called_once()
        with claim_state.locked(paths.lock, wait=False):
            self.assertTrue(paths.lock.is_file())

    def test_saved_replacement_precedes_the_settlement_controller_lock(self) -> None:
        """Hana read_evidence row 12."""
        from scripts.aws_target import controller_lock
        from scripts.aws_target import hana_replacement_record

        _ = hana_replacement_record.record_path(self.state).write_text('{}')
        reads = mock.Mock()
        with (mock.patch.object(hana_replacement_record, 'held',
                               wraps=hana_replacement_record.held) as held,
              mock.patch.object(box, 'control_lock', wraps=controller_lock.control_lock) as lock):
            reads.attach_mock(held, 'replacement')
            reads.attach_mock(lock, 'lock')
            self.assertEqual(self.eligibility(), placement.TemporaryExclusion('busy'))
        self.assertEqual(reads.mock_calls,
                         [('replacement', (self.state,), {}), ('lock', (self.state,), {})])

    def test_execution_read_runs_after_the_target_lock_releases(self) -> None:
        """Hana read_evidence row 39."""
        from scripts.aws_target import hana_execution

        adopted = self.ready()
        paths = claim_state.state_paths(self.target.name)
        read = hana_execution.read_claim_execution

        def read_unlocked(directory: Path, reference: placement.ProviderClaimReference
                          ) -> execution_identity.ExecutionEvidence:
            with claim_state.locked(paths.lock, wait=False):
                return read(directory, reference)

        with mock.patch.object(hana_execution, 'read_claim_execution', side_effect=read_unlocked) as journal:
            evidence = self.evidence(self.request())
        assert isinstance(evidence, placement.FreshHostEvidence)
        self.assertIn(adopted.reserved.claim, evidence.claims)
        journal.assert_called_once_with(self.state, adopted.reserved.claim.reference)

    def test_unmatched_lifecycle_claim_refuses_before_spending_preview(self) -> None:
        """Hana read_evidence row 40."""
        _ = self.ready()
        paths = claim_state.state_paths(self.target.name)
        claim, = claim_state.read_claims(paths)
        claim.path.unlink()
        with (mock.patch.object(hana_spend, 'preview_placement_start',
                               wraps=hana_spend.preview_placement_start) as preview,
              mock.patch.object(box.HanaController, 'image_is_current') as image):
            evidence = self.evidence(self.request())
        assert isinstance(evidence, placement.HostEvidenceUnavailable)
        self.assertEqual(evidence.reason, 'A lifecycle claim has no readable target claim')
        image.assert_not_called()
        preview.assert_not_called()

    def test_starting_lifecycle_precedes_spending_preview(self) -> None:
        """Hana read_evidence row 44."""
        self.assertIsInstance(self.claim(), hana.HanaClaimPending)
        self.clock.advance(1)
        self.assertEqual(self.controller.tick(), box.Lifecycle.STARTING)
        with mock.patch.object(hana_spend, 'preview_placement_start',
                               wraps=hana_spend.preview_placement_start) as preview:
            self.assertEqual(self.eligibility(), placement.TemporaryExclusion('starting'))
        preview.assert_not_called()

    def test_invalid_preview_time_precedes_the_ledger_lock(self) -> None:
        """Hana read_evidence row 48."""
        self.clock.now = -1
        with mock.patch.object(ledger.CostLedger, 'locked_snapshot',
                               wraps=ledger.CostLedger.locked_snapshot) as lock:
            snapshot = self.evidence(self.request())
        assert isinstance(snapshot, placement.FreshHostEvidence)
        self.assertEqual(snapshot.eligibility,
                         placement.TemporaryExclusion('The permit deadline is outside its renewal window'))
        lock.assert_not_called()

    def test_preview_restamps_inputs_after_pricing_before_reusing_result(self) -> None:
        """Hana read_evidence row 52."""
        trace = mock.Mock()
        with (mock.patch.object(hana_spend, '_preview_inputs', wraps=getattr(hana_spend, '_preview_inputs')) as inputs,
              mock.patch.object(ledger.CostLedger, 'locked_snapshot',
                                autospec=True, side_effect=ledger.CostLedger.locked_snapshot) as ledger_read):
            trace.attach_mock(inputs, 'inputs')
            trace.attach_mock(ledger_read, 'price')
            self.assertEqual(self.eligibility(), placement.AllowedHost())
            self.assertEqual(self.eligibility(), placement.AllowedHost())
        self.assertEqual([call[0] for call in trace.mock_calls], ['inputs', 'price', 'inputs', 'inputs'])

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

    def test_ssh_hana_local_requirement_precedes_route_and_lifecycle(self) -> None:
        """SSH dispatch row 7."""
        from scripts.build_targets import targets

        reason = 'fixture requires Linux'
        with (mock.patch.object(hana, 'fixed_policy', wraps=hana.fixed_policy) as policy,
              mock.patch.object(hana, 'controller_for', wraps=hana.controller_for) as controller):
            result = build_targets.ssh_target_eligibility(
                self.target, self.root, 'hana', 'check', targets.LocalRequired(reason))
        self.assertEqual(result, targets.ExcludedTarget(self.target, reason))
        policy.assert_not_called()
        controller.assert_not_called()

    def test_ssh_hana_policy_refusal_precedes_controller_construction(self) -> None:
        """SSH dispatch row 8."""
        from scripts.build_targets import targets

        with (mock.patch.object(hana, 'fixed_policy', wraps=hana.fixed_policy) as policy,
              mock.patch.object(hana, 'controller_for', wraps=hana.controller_for) as controller,
              mock.patch.object(self.controller, 'inspect', wraps=self.controller.inspect) as inspect):
            result = build_targets.ssh_target_eligibility(
                self.target, self.root, 'hana', 'test', targets.ANY_ELIGIBLE_TARGET)
        self.assertEqual(result, targets.ExcludedTarget(self.target, 'command'))
        policy.assert_called_once()
        controller.assert_not_called()
        inspect.assert_not_called()

    def test_ssh_hana_resource_lock_conflict_skips_lifecycle_read(self) -> None:
        """SSH dispatch row 9."""
        from scripts.aws_target import controller_lock
        from scripts.build_targets import targets

        with (hana_command_lock(self.state) as hold,
              mock.patch.object(box, 'hana_work_lock', wraps=controller_lock.hana_work_lock) as lock,
              mock.patch.object(self.controller, 'read_saved_lifecycle',
                               wraps=self.controller.read_saved_lifecycle) as read):
            self.assertIs(hold, HanaCommandAccess.ACQUIRED)
            result = build_targets.ssh_target_eligibility(
                self.target, self.root, 'hana', 'check', targets.ANY_ELIGIBLE_TARGET)
        self.assertEqual(result, targets.ExcludedTarget(self.target, 'busy'))
        lock.assert_called_once_with(self.state)
        read.assert_not_called()

    def test_ssh_hana_unreadable_lifecycle_skips_image_check(self) -> None:
        """SSH dispatch row 10."""
        from scripts.build_targets import targets

        _ = self.controller.path.write_text('{')
        with (mock.patch.object(self.controller, 'read_saved_lifecycle',
                               wraps=self.controller.read_saved_lifecycle) as lifecycle,
              mock.patch.object(self.controller, 'image_is_current',
                               wraps=self.controller.image_is_current) as image):
            result = build_targets.ssh_target_eligibility(
                self.target, self.root, 'hana', 'check', targets.ANY_ELIGIBLE_TARGET)
        self.assertEqual(result, targets.ExcludedTarget(self.target, 'state unreadable'))
        lifecycle.assert_called_once()
        image.assert_not_called()

    def test_ssh_hana_stale_image_precedes_the_busy_lifecycle_refusal(self) -> None:
        """SSH dispatch row 12."""
        from scripts.build_targets import targets

        _ = self.ready()
        (self.state / 'hana/setup.json').unlink()
        calls = mock.Mock()
        with (mock.patch.object(self.controller, 'inspect', wraps=self.controller.inspect) as inspect,
              mock.patch.object(self.controller, 'image_is_current',
                               wraps=self.controller.image_is_current) as image):
            calls.attach_mock(inspect, 'inspect')
            calls.attach_mock(image, 'image')
            result = build_targets.ssh_target_eligibility(
                self.target, self.root, 'hana', 'check', targets.ANY_ELIGIBLE_TARGET)
        self.assertEqual(result, targets.ExcludedTarget(self.target, placement.HANA_IMAGE_STALE))
        self.assertEqual([call[0] for call in calls.mock_calls], ['inspect', 'image'])

    def test_ssh_hana_reports_busy_after_checking_the_current_image(self) -> None:
        """SSH dispatch row 13."""
        from scripts.build_targets import targets

        _ = self.ready()
        with mock.patch.object(self.controller, 'image_is_current',
                               wraps=self.controller.image_is_current) as image:
            result = build_targets.ssh_target_eligibility(
                self.target, self.root, 'hana', 'check', targets.ANY_ELIGIBLE_TARGET)
        self.assertEqual(result, targets.ExcludedTarget(self.target, 'busy'))
        image.assert_called_once()

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

    def test_resource_hold_preserves_the_session_block(self) -> None:
        paths = claim_state.state_paths(self.assignment.destination.target)
        paths.block.parent.mkdir(parents=True, exist_ok=True)
        since = datetime.fromtimestamp(self.clock.now, UTC)
        mac_test.write_block(paths.block, mac_test.ActiveMacBlock(
            'fixture', 'private hold', since, since + timedelta(hours=1),
            mac_test.RecordedSession('another-session', 'fixture'), mac_test.NO_SHOWRUNNER,
            'off_before', mac_test.FREE_MESSAGE_NOT_NEEDED))
        lifecycle = self.controller.inspect_for_settlement()
        assert isinstance(lifecycle, box.HanaLifecycleEvidence)
        for settlement in (lifecycle, box.HanaUnprovisioned()):
            with (self.subTest(settlement=type(settlement).__name__),
                  hana_command_lock(self.state) as access,
                  mock.patch.object(self.controller, 'inspect_for_settlement', return_value=settlement)):
                self.assertIs(access, HanaCommandAccess.ACQUIRED)
                self.assertEqual(self.eligibility(), placement.TemporaryExclusion('blocked'))
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

    def test_claim_rejects_wrong_destination_source_session_and_holder(self) -> None:
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

    def test_assigned_adoption_reads_only_the_repository_claim_paths(self) -> None:
        adopted = self.ready()
        paths = claim_state.state_paths(self.assignment.destination.target)
        scope = self.assignment.destination.scope
        claim = mac_test.claim_path(paths, scope)
        _ = (paths.claims / 'unrelated.json').write_text('{')
        transport = hana.HanaControlTransport(self.workload, self.assignment.destination.host)
        self.assertEqual(adapter.read_assigned_adoption(self.assignment, self.holder, transport), adopted.reserved.claim)

        _ = claim.rename(claim.with_name(f'{claim.stem}.slot-1.json'))
        self.assertEqual(adapter.read_assigned_adoption(self.assignment, self.holder, transport), adopted.reserved.claim)

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

    def test_assignment_resolves_registry_before_transport_settings(self) -> None:
        """Hana validate_assignment row 1."""
        calls = mock.Mock()
        with (mock.patch.object(target_config, 'configured_ssh_target',
                               wraps=target_config.configured_ssh_target) as target,
              mock.patch.object(target_config, 'read_execution_transport',
                               wraps=target_config.read_execution_transport) as transport):
            calls.attach_mock(target, 'target')
            calls.attach_mock(transport, 'transport')
            result = hana.validate_assignment(self.assignment, self.holder)
        self.assertEqual(result, hana.HanaControlTransport(self.workload, self.assignment.destination.host))
        self.assertEqual(calls.mock_calls, [('target', (self.target.name,), {}), ('transport', (self.target,), {})])

    def test_assignment_rejects_ordinary_ssh_before_loading_hana_policy(self) -> None:
        """Hana validate_assignment row 2."""
        text_setting(self.workload, 'execution_transport', 'ssh')
        with mock.patch.object(adapter, 'read_settings', wraps=adapter.read_settings) as policy:
            result = hana.validate_assignment(self.assignment, self.holder)
        self.assertEqual(result, hana.HanaClaimRefused('The assigned target does not use Hana control'))
        policy.assert_not_called()

    def test_assignment_reopens_the_workload_after_transport_selection(self) -> None:
        """Hana validate_assignment row 3."""
        read = target_config.read_execution_transport

        from scripts.build_targets.execution_transport import ExecutionTransport

        def choose(target: target_config.SshTarget) -> ExecutionTransport:
            transport = read(target)
            text_setting(self.workload, 'repository_identity', 'https://github.com/fixture/another')
            return transport

        with mock.patch.object(target_config, 'read_execution_transport', side_effect=choose) as transport:
            result = hana.validate_assignment(self.assignment, self.holder)
        self.assertEqual(result, hana.HanaClaimRefused('The assignment differs from the configured Hana scope'))
        transport.assert_called_once_with(self.target)

    def test_assignment_switch_refusal_precedes_holder_validation(self) -> None:
        """Hana validate_assignment row 4."""
        text_setting(self.workload, 'enabled', 'off')
        with mock.patch.object(process_start, 'process_start', wraps=process_start.process_start) as holder:
            result = hana.validate_assignment(self.assignment, self.holder)
        self.assertEqual(result, hana.HanaClaimRefused('Hana is disabled'))
        holder.assert_not_called()

    def test_assignment_scope_refusal_precedes_holder_validation(self) -> None:
        """Hana validate_assignment row 5."""
        destination = replace(self.assignment.destination, slot=2)
        with mock.patch.object(process_start, 'process_start', wraps=process_start.process_start) as holder:
            result = hana.validate_assignment(replace(self.assignment, destination=destination), self.holder)
        self.assertEqual(result, hana.HanaClaimRefused('The assignment differs from the configured Hana scope'))
        holder.assert_not_called()

    def test_claim_refuses_controller_workload_before_taking_target_lock(self) -> None:
        """Hana claim_assigned row 1."""
        paths = claim_state.state_paths(self.target.name)
        before = self.controller.path.read_bytes()
        other = box.HanaController(self.state, self.settings, self.root / 'other.conf', self.provider)
        with mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock:
            result = hana.claim_assigned(self.assignment, self.holder, other, worktree=str(self.root))
        self.assertEqual(result, hana.HanaClaimRefused('The lifecycle controller differs from the assigned workload'))
        lock.assert_not_called()
        self.assertFalse(paths.directory.exists())
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_claim_resolves_registration_again_before_target_lock(self) -> None:
        """Hana claim_assigned row 2."""
        paths = claim_state.state_paths(self.target.name)
        calls = mock.Mock()
        with (mock.patch.object(target_config, 'configured_ssh_target',
                               wraps=target_config.configured_ssh_target) as target,
              mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock):
            calls.attach_mock(target, 'target')
            calls.attach_mock(lock, 'lock')
            self.assertIsInstance(self.claim(), hana.HanaClaimPending)
        self.assertEqual(calls.mock_calls[:3],
                         [('target', (self.target.name,), {}), ('target', (self.target.name,), {}),
                          ('lock', (paths.lock,), {})])

    def test_claim_reads_target_claims_before_considering_the_controller(self) -> None:
        """Hana claim_assigned row 4."""
        from scripts.aws_target import controller_lock
        paths = claim_state.state_paths(self.target.name)
        paths.claims.mkdir(parents=True)
        _ = (paths.claims / 'bad.json').write_text('{')
        before = self.controller.path.read_bytes()
        with (mock.patch.object(box, 'hana_work_lock', wraps=controller_lock.hana_work_lock) as lock,
              self.assertRaises(claim_state.StateReadError)):
            _ = self.claim()
        lock.assert_not_called()
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_foreign_target_claim_refuses_before_controller_lock(self) -> None:
        """Hana claim_assigned row 5."""
        from scripts.aws_target import controller_lock
        self.assertIsInstance(self.claim(), hana.HanaClaimPending)
        before = self.controller.path.read_bytes()
        other = replace(self.assignment, episode='another-episode')
        with (mock.patch.object(box, 'hana_work_lock', wraps=controller_lock.hana_work_lock) as lock,
              mock.patch.object(self.controller, 'request', wraps=self.controller.request) as request):
            result = hana.claim_assigned(other, self.holder, self.controller, worktree=str(self.root))
        self.assertEqual(result, hana.HanaClaimRefused(box.HANA_CLAIM_BUSY))
        lock.assert_not_called()
        request.assert_not_called()
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_claim_writes_execution_guard_before_reading_lifecycle(self) -> None:
        """Hana claim_assigned row 10."""
        from scripts.aws_target import hana_execution

        execution = execution_identity.assigned_claim_id(self.assignment.episode, self.assignment.attempt)
        guard = hana_execution.execution_guard_path(self.state, execution)
        original_read = cast(Callable[[], box.BoxRecord | box.NoDeployment], getattr(self.controller, '_read'))

        def read_lifecycle() -> box.BoxRecord | box.NoDeployment:
            self.assertEqual(guard.read_text(), execution + '\n')
            return original_read()

        self.assertFalse(guard.exists())
        with mock.patch.object(self.controller, '_read', side_effect=read_lifecycle) as read:
            self.assertIsInstance(self.claim(), hana.HanaClaimPending)
        self.assertEqual(read.call_count, 2)

    def test_claim_reads_lifecycle_and_spending_before_stale_image_refusal(self) -> None:
        """Hana claim_assigned row 11."""
        (self.state / 'hana/setup.json').unlink()
        saved = self.controller.path.read_bytes()
        calls = mock.Mock()
        with (mock.patch.object(self.controller, '_read', wraps=getattr(self.controller, '_read')) as lifecycle,
              mock.patch.object(self.controller, '_spending', wraps=getattr(self.controller, '_spending')) as spending,
              mock.patch.object(self.controller, 'image_is_current',
                               wraps=self.controller.image_is_current) as image,
              mock.patch.object(self.controller, '_save', wraps=getattr(self.controller, '_save')) as save):
            calls.attach_mock(lifecycle, 'lifecycle')
            calls.attach_mock(spending, 'spending')
            calls.attach_mock(image, 'image')
            self.assertEqual(self.claim(), hana.HanaClaimRefused(placement.HANA_IMAGE_STALE))
        self.assertEqual([call[0] for call in calls.mock_calls], ['lifecycle', 'spending', 'image'])
        save.assert_not_called()
        self.assertEqual(self.controller.path.read_bytes(), saved)

    def test_claim_reads_owner_start_before_saving_the_lifecycle_claim(self) -> None:
        """Hana claim_assigned row 12."""
        before = self.controller.path.read_bytes()
        starts: list[int] = []

        def read_start(pid: int) -> process_start.ProcessStart:
            self.assertEqual(self.controller.path.read_bytes(), before)
            starts.append(pid)
            if len(starts) == 1:
                return process_start.Started(self.holder.start_time)
            return process_start.Gone()

        with mock.patch.object(process_start, 'process_start', side_effect=read_start):
            result = self.claim()
        assert isinstance(result, hana.HanaClaimPending)
        self.assertEqual(starts, [self.holder.pid, os.getpid()])
        self.assertEqual(result.claim.owner, box.UnrecordedClaimOwner())

    def test_claim_saves_lifecycle_before_inspection_and_target_publication(self) -> None:
        """Hana claim_assigned row 13."""
        paths = claim_state.state_paths(self.target.name)
        reference = placement.assigned_claim_reference(self.assignment)
        inspect = self.controller.inspect

        def inspect_published() -> box.HanaInspection:
            snapshot = inspect()
            assert isinstance(snapshot, box.HanaLifecycleEvidence)
            self.assertEqual([row.execution for row in snapshot.claims], [reference.claim_id])
            self.assertEqual(claim_state.read_claims(paths), ())
            return snapshot

        with mock.patch.object(self.controller, 'inspect', side_effect=inspect_published) as read:
            self.assertIsInstance(self.claim(), hana.HanaClaimPending)
        read.assert_called_once()
        claim, = claim_state.read_claims(paths)
        self.assertEqual(claim.record['claim_id'], reference.claim_id)

    def test_claim_resource_hold_refuses_before_inspection_and_publication(self) -> None:
        """Hana claim_assigned row 14."""
        paths = claim_state.state_paths(self.target.name)
        with (hana_command_lock(self.state) as hold,
              mock.patch.object(self.controller, 'inspect', wraps=self.controller.inspect) as inspect):
            self.assertIs(hold, HanaCommandAccess.ACQUIRED)
            self.assertIsInstance(self.claim(), hana.HanaClaimResourcesHeld)
        inspect.assert_not_called()
        self.assertEqual(claim_state.read_claims(paths), ())

    def test_claim_retains_lifecycle_commit_when_refreshed_read_fails(self) -> None:
        """Hana claim_assigned row 15."""
        paths = claim_state.state_paths(self.target.name)
        reference = placement.assigned_claim_reference(self.assignment)
        request = self.controller.request

        def commit_then_damage(execution: str, *, assigned: bool = False
                               ) -> box.ClaimCommitted | box.ClaimRefused | box.HanaResourcesHeld:
            result = request(execution, assigned=assigned)
            self.assertIsInstance(result, box.ClaimCommitted)
            _ = self.controller.path.write_text('{')
            return result

        with (mock.patch.object(self.controller, 'request', side_effect=commit_then_damage),
              mock.patch.object(self.controller, 'inspect', wraps=self.controller.inspect) as inspect):
            self.assertEqual(self.claim(), hana.HanaClaimRetained(reference, 'The Hana lifecycle evidence is unavailable'))
        inspect.assert_called_once()
        self.assertEqual(claim_state.read_claims(paths), ())

    def test_release_checks_journal_before_registration_and_target_lock(self) -> None:
        """Hana release_assigned row 1."""
        _ = self.ready()
        before = self.controller.path.read_bytes()
        paths = claim_state.state_paths(self.target.name)
        claim, = claim_state.read_claims(paths)
        with (mock.patch.object(target_config, 'configured_ssh_target',
                               wraps=target_config.configured_ssh_target) as target,
              mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock):
            result = hana.release_assigned(self.assignment, self.holder, self.controller)
        self.assertEqual(result, hana.HanaClaimReleaseRefused('The Hana command settlement is unconfirmed'))
        target.assert_not_called()
        lock.assert_not_called()
        self.assertEqual(self.controller.path.read_bytes(), before)
        self.assertTrue(claim.path.is_file())

    def test_release_reads_transport_after_the_settled_journal(self) -> None:
        """Hana release_assigned row 2."""
        from scripts.aws_target import hana_execution

        adopted = self.ready()
        reference = adopted.reserved.claim.reference
        closure = hana_execution.settle_unpublished_claim(self.state, reference,
            release_claim=lambda: hana.HanaClaimReleaseRefused('fixture holds the claim'), reason='fixture')
        self.assertIsInstance(closure, hana_execution.HanaCleanupPending)
        before = self.controller.path.read_bytes()
        text_setting(self.workload, 'execution_transport', 'ssh')
        calls = mock.Mock()
        with (mock.patch.object(hana_execution, 'execution_command_settled',
                               wraps=hana_execution.execution_command_settled) as journal,
              mock.patch.object(target_config, 'configured_ssh_target',
                               wraps=target_config.configured_ssh_target) as target,
              mock.patch.object(target_config, 'read_execution_transport',
                               wraps=target_config.read_execution_transport) as transport,
              mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock):
            calls.attach_mock(journal, 'journal')
            calls.attach_mock(target, 'target')
            calls.attach_mock(transport, 'transport')
            result = hana.release_assigned(self.assignment, self.holder, self.controller)
        self.assertEqual(result, hana.HanaClaimReleaseRefused('The assigned Hana transport differs from the lifecycle controller'))
        self.assertEqual(calls.mock_calls, [('journal', (self.state, reference.claim_id,), {}),
                                           ('target', (self.target.name,), {}), ('transport', (self.target,), {})])
        lock.assert_not_called()
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_release_refuses_changed_host_before_locking_or_deleting(self) -> None:
        """Hana release_assigned row 3."""
        from scripts.aws_target import hana_execution

        adopted = self.ready()
        reference = adopted.reserved.claim.reference
        closure = hana_execution.settle_unpublished_claim(self.state, reference,
            release_claim=lambda: hana.HanaClaimReleaseRefused('fixture holds the claim'), reason='fixture')
        self.assertIsInstance(closure, hana_execution.HanaCleanupPending)
        before = self.controller.path.read_bytes()
        claim, = claim_state.read_claims(claim_state.state_paths(self.target.name))
        text_setting(self.workload, 'host', 'changed-host')
        with mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock:
            result = hana.release_assigned(self.assignment, self.holder, self.controller)
        self.assertEqual(result, hana.HanaClaimReleaseRefused('The assigned Hana transport differs from the lifecycle controller'))
        lock.assert_not_called()
        self.assertTrue(claim.path.is_file())
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_release_resolves_registration_again_before_locking(self) -> None:
        """Hana release_assigned row 4."""
        from scripts.aws_target import hana_execution

        adopted = self.ready()
        reference = adopted.reserved.claim.reference
        closure = hana_execution.settle_unpublished_claim(self.state, reference,
            release_claim=lambda: hana.HanaClaimReleaseRefused('fixture holds the claim'), reason='fixture')
        self.assertIsInstance(closure, hana_execution.HanaCleanupPending)
        paths = claim_state.state_paths(self.target.name)
        calls = mock.Mock()
        with (mock.patch.object(target_config, 'configured_ssh_target',
                               wraps=target_config.configured_ssh_target) as target,
              mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock):
            calls.attach_mock(target, 'target')
            calls.attach_mock(lock, 'lock')
            result = hana.release_assigned(self.assignment, self.holder, self.controller)
        self.assertEqual(result, hana.HanaClaimReleased())
        self.assertEqual(calls.mock_calls, [('target', (self.target.name,), {}), ('target', (self.target.name,), {}),
                                           ('lock', (paths.lock,), {})])
        self.assertEqual(claim_state.read_claims(paths), ())

    def test_release_claim_read_failure_precedes_lifecycle_changes(self) -> None:
        """Hana release_assigned row 6."""
        from scripts.aws_target import hana_execution

        adopted = self.ready()
        reference = adopted.reserved.claim.reference
        closure = hana_execution.settle_unpublished_claim(self.state, reference,
            release_claim=lambda: hana.HanaClaimReleaseRefused('fixture holds the claim'), reason='fixture')
        self.assertIsInstance(closure, hana_execution.HanaCleanupPending)
        before = self.controller.path.read_bytes()
        claim, = claim_state.read_claims(claim_state.state_paths(self.target.name))
        _ = claim.path.write_text('{')
        with (mock.patch.object(self.controller, 'inspect_for_settlement',
                                wraps=self.controller.inspect_for_settlement) as inspect,
              self.assertRaises(claim_state.StateReadError)):
            _ = hana.release_assigned(self.assignment, self.holder, self.controller)
        inspect.assert_not_called()
        self.assertEqual(claim.path.read_text(), '{')
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_wrong_release_holder_preserves_target_and_lifecycle_records(self) -> None:
        """Hana release_assigned row 7."""
        from scripts.aws_target import hana_execution

        adopted = self.ready()
        reference = adopted.reserved.claim.reference
        closure = hana_execution.settle_unpublished_claim(self.state, reference,
            release_claim=lambda: hana.HanaClaimReleaseRefused('fixture holds the claim'), reason='fixture')
        self.assertIsInstance(closure, hana_execution.HanaCleanupPending)
        before = self.controller.path.read_bytes()
        claim, = claim_state.read_claims(claim_state.state_paths(self.target.name))
        claim_before = claim.path.read_bytes()
        with mock.patch.object(self.controller, 'inspect_for_settlement',
                               wraps=self.controller.inspect_for_settlement) as inspect:
            result = hana.release_assigned(self.assignment, replace(self.holder, start_time='other'), self.controller)
        self.assertEqual(result, hana.HanaClaimReleaseRefused('The local Hana claim differs from the assignment or holder'))
        inspect.assert_not_called()
        self.assertEqual(claim.path.read_bytes(), claim_before)
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_attached_claim_reads_unpublished_evidence_before_attempt_lock(self) -> None:
        """Hana read_evidence row 23."""
        from scripts.aws_target import hana_execution

        adopted = self.ready()
        reference = adopted.reserved.claim.reference
        before = self.controller.path.read_bytes()
        with (mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()),
              mock.patch.object(hana_execution, 'read_claim_execution',
                                wraps=hana_execution.read_claim_execution) as read,
              mock.patch.object(hana_execution, '_journal_lock',
                                wraps=getattr(hana_execution, '_journal_lock')) as lock):
            evidence = self.evidence(replace(self.request(), episode='other'))
        assert isinstance(evidence, placement.FreshHostEvidence)
        self.assertEqual(evidence.eligibility, placement.TemporaryExclusion('busy'))
        read.assert_has_calls([mock.call(self.state, reference), mock.call(self.state, reference)])
        self.assertEqual(read.call_count, 2)
        lock.assert_not_called()
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_reading_relocks_the_controller_for_attached_release(self) -> None:
        """Hana read_evidence row 29."""
        from scripts.aws_target import controller_lock
        from scripts.aws_target import hana_execution

        adopted = self.ready()
        reference = adopted.reserved.claim.reference
        closure = hana_execution.settle_unpublished_claim(self.state, reference,
            release_claim=lambda: hana.HanaClaimReleaseRefused('fixture holds the claim'), reason='fixture')
        self.assertIsInstance(closure, hana_execution.HanaCleanupPending)
        calls = mock.Mock()
        with (mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()),
              mock.patch.object(box, 'control_lock', wraps=controller_lock.control_lock) as lock,
              mock.patch.object(self.controller, 'release', wraps=self.controller.release) as release):
            calls.attach_mock(lock, 'lock')
            calls.attach_mock(release, 'release')
            evidence = self.evidence(self.request())
        assert isinstance(evidence, placement.FreshHostEvidence)
        self.assertEqual(evidence.claims, ())
        self.assertEqual(calls.mock_calls,
                         [('lock', (self.state,), {}), ('release', (reference.claim_id,), {}), ('lock', (self.state,), {})])

    def test_attached_release_rechecks_the_journal_before_saving(self) -> None:
        """Hana read_evidence row 30."""
        from scripts.aws_target import hana_execution

        adopted = self.ready()
        reference = adopted.reserved.claim.reference
        closure = hana_execution.settle_unpublished_claim(self.state, reference,
            release_claim=lambda: hana.HanaClaimReleaseRefused('fixture holds the claim'), reason='fixture')
        self.assertIsInstance(closure, hana_execution.HanaCleanupPending)
        journal = hana_execution.execution_path(self.state, reference.claim_id)
        before = self.controller.path.read_bytes()
        release = self.controller.release

        def release_after_lost_journal(execution: str) -> hana.HanaClaimRelease:
            journal.unlink()
            return release(execution)

        with (mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()),
              mock.patch.object(self.controller, 'release', side_effect=release_after_lost_journal) as called,
              mock.patch.object(self.controller, '_save', wraps=getattr(self.controller, '_save')) as save):
            evidence = self.evidence(self.request())
        assert isinstance(evidence, placement.FreshHostEvidence)
        called.assert_called_once_with(reference.claim_id)
        save.assert_not_called()
        self.assertEqual(evidence.claims, (adopted.reserved.claim,))
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_attached_release_saves_idle_state_before_refresh(self) -> None:
        """Hana read_evidence row 31."""
        from scripts.aws_target import hana_execution

        adopted = self.ready()
        reference = adopted.reserved.claim.reference
        closure = hana_execution.settle_unpublished_claim(self.state, reference,
            release_claim=lambda: hana.HanaClaimReleaseRefused('fixture holds the claim'), reason='fixture')
        self.assertIsInstance(closure, hana_execution.HanaCleanupPending)
        self.clock.advance(1)
        inspect = self.controller.inspect
        observed_claims: list[tuple[box.WorkClaim, ...]] = []

        def inspect_release() -> box.HanaInspection:
            result = inspect()
            assert isinstance(result, box.HanaLifecycleEvidence)
            observed_claims.append(result.claims)
            if len(observed_claims) == 2:
                self.assertEqual(result.permit, box.NoDeliveredPermit())
                row = cast(dict[str, object], json.loads(self.controller.path.read_bytes()))
                self.assertEqual(row['idle_since'], self.clock.now)
            return result

        with (mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()),
              mock.patch.object(self.controller, 'inspect', side_effect=inspect_release)):
            evidence = self.evidence(self.request())
        assert isinstance(evidence, placement.FreshHostEvidence)
        self.assertEqual(evidence.claims, ())
        self.assertEqual([[row.execution for row in claims] for claims in observed_claims],
                         [[reference.claim_id], []])

    def test_failed_refresh_after_attached_release_skips_spending(self) -> None:
        """Hana read_evidence row 36."""
        from scripts.aws_target import hana_execution

        adopted = self.ready()
        reference = adopted.reserved.claim.reference
        closure = hana_execution.settle_unpublished_claim(self.state, reference,
            release_claim=lambda: hana.HanaClaimReleaseRefused('fixture holds the claim'), reason='fixture')
        self.assertIsInstance(closure, hana_execution.HanaCleanupPending)
        release = self.controller.release

        def release_then_damage(execution: str) -> hana.HanaClaimRelease:
            result = release(execution)
            self.assertEqual(result, hana.HanaClaimReleased())
            _ = self.controller.path.write_text('{')
            return result

        with (mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()),
              mock.patch.object(self.controller, 'release', side_effect=release_then_damage) as released,
              mock.patch.object(hana_spend, 'preview_placement_start',
                               wraps=hana_spend.preview_placement_start) as preview):
            evidence = self.evidence(self.request())
        assert isinstance(evidence, placement.HostEvidenceUnavailable)
        self.assertEqual(evidence.reason, 'The Hana lifecycle evidence is unavailable')
        released.assert_called_once_with(reference.claim_id)
        preview.assert_not_called()

    def test_foreign_lifecycle_claim_keeps_diagnostic_and_skips_preview(self) -> None:
        """Hana read_evidence row 38."""
        adopted = self.ready()
        claim, = claim_state.read_claims(claim_state.state_paths(self.target.name))
        with mock.patch.object(hana_spend, 'preview_placement_start',
                               wraps=hana_spend.preview_placement_start) as preview:
            evidence = self.evidence(replace(self.request(), episode='other'))
        assert isinstance(evidence, placement.FreshHostEvidence)
        self.assertEqual(evidence.eligibility, placement.TemporaryExclusion('busy'))
        self.assertEqual(evidence.claims, (adopted.reserved.claim,))
        self.assertEqual(evidence.holder, placement.ResourceHolderSession(claim.record['submitting_session']))
        self.assertEqual(evidence.provider_diagnostic,
                         placement.BusyProviderDiagnostic(self.target.name, claim.record['what'], claim.record['since']))
        preview.assert_not_called()

    def test_recovery_checks_the_live_holder_before_resolving_claim_paths(self) -> None:
        """Hana recover_assigned_execution row 1."""
        from scripts.aws_target import hana_execution

        with (mock.patch.object(claim_state, 'state_paths', wraps=claim_state.state_paths) as paths,
              mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock):
            result = adapter.recover_assigned_execution(self.assignment, self.holder, self.controller)
        self.assertEqual(result, hana_execution.HanaSettlementPending('The Hana claim holder has not ended'))
        paths.assert_not_called()
        lock.assert_not_called()

    def test_recovery_resolves_target_before_locking(self) -> None:
        """Hana recover_assigned_execution row 2."""
        from scripts.aws_target import hana_execution

        paths = claim_state.state_paths(self.target.name)
        trace = mock.Mock()
        with (mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()),
              mock.patch.object(target_config, 'configured_ssh_target',
                                wraps=target_config.configured_ssh_target) as target,
              mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock):
            trace.attach_mock(target, 'target')
            trace.attach_mock(lock, 'lock')
            result = adapter.recover_assigned_execution(self.assignment, self.holder, self.controller)
        self.assertEqual(result, hana_execution.HanaSettlementPending('The assigned Hana dispatch journal is unavailable'))
        self.assertEqual(trace.mock_calls, [('target', (self.target.name,), {}), ('lock', (paths.lock,), {})])

    def test_recovery_refuses_a_changed_target_claim_before_controller_lock(self) -> None:
        """Hana recover_assigned_execution row 4."""
        from scripts.aws_target import hana_execution

        _ = self.ready()
        paths = claim_state.state_paths(self.target.name)
        claim, = claim_state.read_claims(paths)
        changed = {**claim.record, 'remote_host': 'different-host'}
        _ = claim.path.write_text(json.dumps(changed))
        before = self.controller.path.read_bytes()
        with (mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()),
              mock.patch.object(self.controller, 'inspect_for_settlement',
                                wraps=self.controller.inspect_for_settlement) as inspect):
            result = adapter.recover_assigned_execution(self.assignment, self.holder, self.controller)
        self.assertEqual(result, hana_execution.HanaSettlementPending('The Hana claim differs from the assignment or holder'))
        inspect.assert_not_called()
        self.assertEqual(self.controller.path.read_bytes(), before)
        self.assertEqual(claim.path.read_text(), json.dumps(changed))

    def test_recovery_retains_a_lifecycle_claim_before_attempt_lock(self) -> None:
        """Hana recover_assigned_execution row 6."""
        from scripts.aws_target import hana_execution

        _ = self.ready()
        claim, = claim_state.read_claims(claim_state.state_paths(self.target.name))
        claim.path.unlink()
        before = self.controller.path.read_bytes()
        with (mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()),
              mock.patch.object(self.controller, 'inspect_for_settlement',
                                wraps=self.controller.inspect_for_settlement) as inspect,
              mock.patch.object(hana_execution, '_journal_lock',
                                wraps=getattr(hana_execution, '_journal_lock')) as journal):
            result = adapter.recover_assigned_execution(self.assignment, self.holder, self.controller)
        self.assertEqual(result, hana_execution.HanaSettlementPending('The Hana lifecycle claim remains unresolved'))
        inspect.assert_called_once()
        journal.assert_not_called()
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_recovery_reads_journal_after_releasing_target_lock(self) -> None:
        """Hana recover_assigned_execution row 7."""
        from scripts.aws_target import hana_execution, hana_protocol
        from scripts.aws_target.test_hana_execution import HanaExecutionTests

        template = HanaExecutionTests().request
        source = self.assignment.source_key
        assert isinstance(source, execution_identity.SourceKeyKnown)
        request = replace(template, source_key=source)
        record: dict[str, object] = {
            'version': 1, 'phase': 'fenced', 'request': hana_protocol.request_fields(request),
            'publication_attempted': False, 'command_settled': True,
            'evidence': hana_protocol.encode_evidence(execution_identity.ExecutionNotStarted()),
            'outcome': {'kind': 'prelaunch', 'reason': 'fixture closure'},
        }
        journal = hana_execution.execution_path(self.state, request.key.claim_id)
        journal.parent.mkdir(parents=True)
        _ = journal.write_text(json.dumps(record))
        read = cast(Callable[[Path, str], dict[str, object]], getattr(hana_execution, '_read_journal'))
        changed = replace(self.assignment, source_key=execution_identity.SourceKeyKnown('changed-source'))

        paths = claim_state.state_paths(self.target.name)

        def read_unlocked(directory: Path, claim_id: str) -> dict[str, object]:
            with claim_state.locked(paths.lock, wait=False):
                return read(directory, claim_id)

        with (mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()),
              mock.patch.object(hana_execution, '_read_journal', side_effect=read_unlocked) as read_journal,
              mock.patch.object(hana_execution, '_journal_lock',
                                wraps=getattr(hana_execution, '_journal_lock')) as journal_lock):
            result = adapter.recover_assigned_execution(changed, self.holder, self.controller)
        self.assertEqual(result, hana_execution.HanaSettlementPending('The Hana journal differs from the assignment'))
        read_journal.assert_called_once_with(self.state, request.key.claim_id)
        journal_lock.assert_not_called()
        self.assertEqual(journal.read_text(), json.dumps(record))

    def test_settled_command_recovery_takes_the_exact_attempt_lock(self) -> None:
        """Hana recover_assigned_execution row 8."""
        from scripts.aws_target import hana_execution, hana_protocol
        from scripts.aws_target.test_hana_execution import HanaExecutionTests

        template = HanaExecutionTests().request
        source = self.assignment.source_key
        assert isinstance(source, execution_identity.SourceKeyKnown)
        request = replace(template, source_key=source)
        record: dict[str, object] = {
            'version': 1, 'phase': 'fenced', 'request': hana_protocol.request_fields(request),
            'publication_attempted': False, 'command_settled': True,
            'evidence': hana_protocol.encode_evidence(execution_identity.ExecutionNotStarted()),
            'outcome': {'kind': 'prelaunch', 'reason': 'fixture closure'},
        }
        journal = hana_execution.execution_path(self.state, request.key.claim_id)
        journal.parent.mkdir(parents=True)
        _ = journal.write_text(json.dumps(record))
        trace = mock.Mock()
        with (mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()),
              mock.patch.object(hana_execution, '_read_journal',
                                wraps=getattr(hana_execution, '_read_journal')) as read,
              mock.patch.object(hana_execution, '_journal_lock',
                                wraps=getattr(hana_execution, '_journal_lock')) as lock):
            trace.attach_mock(read, 'read')
            trace.attach_mock(lock, 'lock')
            result = adapter.recover_assigned_execution(self.assignment, self.holder, self.controller)
        self.assertIsInstance(result, hana_execution.HanaSettled)
        self.assertEqual([call[0] for call in trace.mock_calls], ['read', 'lock', 'read'])
        lock.assert_called_once_with(self.state, request.key.claim_id)

    def test_recovery_refuses_journal_changed_under_attempt_lock(self) -> None:
        """Hana recover_assigned_execution row 9."""
        from scripts.aws_target import hana_execution, hana_protocol
        from scripts.aws_target.test_hana_execution import HanaExecutionTests

        template = HanaExecutionTests().request
        source = self.assignment.source_key
        assert isinstance(source, execution_identity.SourceKeyKnown)
        request = replace(template, source_key=source)
        record: dict[str, object] = {
            'version': 1, 'phase': 'fenced', 'request': hana_protocol.request_fields(request),
            'publication_attempted': False, 'command_settled': True,
            'evidence': hana_protocol.encode_evidence(execution_identity.ExecutionNotStarted()),
            'outcome': {'kind': 'prelaunch', 'reason': 'fixture closure'},
        }
        journal = hana_execution.execution_path(self.state, request.key.claim_id)
        journal.parent.mkdir(parents=True)
        _ = journal.write_text(json.dumps(record))
        from scripts.aws_target import controller_lock

        @contextmanager
        def lock_after_edit(directory: Path, claim_id: str) -> Generator[None]:
            self.assertEqual((directory, claim_id), (self.state, request.key.claim_id))
            with controller_lock.control_lock(journal.with_suffix('.lock')):
                record['command_settled'] = False
                _ = journal.write_text(json.dumps(record))
                yield

        with (mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()),
              mock.patch.object(hana_execution, '_journal_lock', side_effect=lock_after_edit),
              mock.patch.object(hana_execution, '_save_journal',
                                wraps=getattr(hana_execution, '_save_journal')) as save):
            result = adapter.recover_assigned_execution(self.assignment, self.holder, self.controller)
        self.assertEqual(result, hana_execution.HanaSettlementPending('The Hana journal has no exact command settlement'))
        save.assert_not_called()
        self.assertEqual(journal.read_text(), json.dumps(record))

    def test_recovery_saves_settled_phase_once_and_returns_terminal_result(self) -> None:
        """Hana recover_assigned_execution row 10."""
        from scripts.aws_target import hana_execution, hana_protocol
        from scripts.aws_target.test_hana_execution import HanaExecutionTests

        template = HanaExecutionTests().request
        source = self.assignment.source_key
        assert isinstance(source, execution_identity.SourceKeyKnown)
        request = replace(template, source_key=source)
        record: dict[str, object] = {
            'version': 1, 'phase': 'fenced', 'request': hana_protocol.request_fields(request),
            'publication_attempted': False, 'command_settled': True,
            'evidence': hana_protocol.encode_evidence(execution_identity.ExecutionNotStarted()),
            'outcome': {'kind': 'prelaunch', 'reason': 'fixture closure'},
        }
        journal = hana_execution.execution_path(self.state, request.key.claim_id)
        journal.parent.mkdir(parents=True)
        _ = journal.write_text(json.dumps(record))
        before = self.controller.path.read_bytes()
        with (mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()),
              mock.patch.object(hana_execution, '_save_journal',
                                wraps=getattr(hana_execution, '_save_journal')) as save):
            result = adapter.recover_assigned_execution(self.assignment, self.holder, self.controller)
            replay = adapter.recover_assigned_execution(self.assignment, self.holder, self.controller)
        expected = hana_execution.HanaSettled(placement.PrelaunchRefused('fixture closure'),
                                             execution_identity.ExecutionNotStarted())
        self.assertEqual((result, replay), (expected, expected))
        save.assert_called_once()
        self.assertEqual(journal.read_text(), json.dumps({**record, 'phase': 'settled'}, sort_keys=True) + '\n')
        self.assertEqual(self.controller.path.read_bytes(), before)

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


class HanaControllerReadingTests(CostFixture):
    def test_controller_paths_resolve_before_the_claim_directory_exists(self) -> None:
        """Hana read_evidence row 5."""
        workload = self.root / 'hana_aws.conf'
        install_hana_spending_settings(workload)
        _ = workload.write_text(workload.read_text()
            + f'execution_transport=hana-control\nrepository_identity={KEY}\nkinds=check\n'
            + '# whole: 1..1\nmax_steps=1\n')
        target = target_config.SshTarget('fixture', 'linux', workload)
        paths = claim_state.StatePaths(self.root / 'targets/fixture', target.name, platform='linux')
        reads = mock.Mock()

        def resolve_directory(name: str) -> Path:
            self.assertFalse(paths.directory.exists())
            return self.state if name == 'aws' else paths.directory

        destination = placement.PlacementDestination('ssh', target.name, 'fixture-hana',
            claim_state.RepositoryScope('hana', KEY), 1)
        session = OwningSession('fixture-session')
        command = ('cargo', 'check')
        key = placement.HostEvidenceKey(destination, session, command, 'fixture',
                                        str(self.root), 'hana', 'check', command_kind='check')
        constraints = placement.PlacementConstraints(execution_identity.SourceKeyKnown('source'),
            command, str(self.root), 'hana', session, (placement.PlacementCandidate(destination),),
            command_kind='check')
        request = wire.PlacementRequest('fixture-episode', placement.SubmittingOwner('fixture', 123, 'start'), 0,
            records.StepProfile('hana', 'check', str(self.root), records.ExpectedPeak(1024, 'fallback', 0)),
            constraints)
        settings = conf.read(workload)
        with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
              mock.patch.object(eligibility, 'repository_identity',
                               return_value=eligibility.KnownRepository('hana', KEY)),
              mock.patch.object(target_config, 'ssh_target_state_directory',
                                side_effect=resolve_directory) as directories,
              mock.patch.object(conf, 'path', wraps=conf.path) as setting_paths):
            reads.attach_mock(directories, 'directory')
            reads.attach_mock(setting_paths, 'config')
            snapshot = hana.read_evidence(key, request,
                transport=hana.HanaControlTransport(workload, 'fixture-hana'), workload=settings)
        assert isinstance(snapshot, placement.FreshHostEvidence)
        self.assertEqual(snapshot.eligibility, placement.TemporaryExclusion('unprovisioned'))
        self.assertEqual(reads.mock_calls,
                         [('directory', ('aws',), {}), ('config', ('aws_target',), {}), ('directory', (target.name,), {})])
        self.assertTrue(paths.lock.is_file())


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
            'enabled=on\nexecution_transport=hana-gpu\nhana_gpu_route_enabled=on\n'
            + f'hana_gpu_repository={KEY}\nhana_gpu_instance_types=g4dn.2xlarge g6.2xlarge\n'
            + 'hana_gpu_selections=hana_mimesis_tools:shader_variants\n# range: 1..60\napi_timeout_s=30\n')
        self.account_dir.mkdir(parents=True)
        self.grants = self.account_dir / 'gate-deployment.json'
        _ = self.grants.write_text(json.dumps({'gpu_instance_types': ['g4dn.2xlarge', 'g6.2xlarge']}))
        _ = (self.root / 'build_targets.conf').write_text(
            'targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n'
            + 'pinned_targets=hana-gpu\nkind.hana-gpu=ssh\nplatform.hana-gpu=linux\n'
            + f'settings.hana-gpu={gpu_target.settings}\n' + UNREACHABLE_WAIT_FIXTURE)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {'CONF_DIR': str(self.root)})))
        _ = self.enterContext(mock.patch.object(eligibility, 'repository_identity',
            return_value=eligibility.KnownRepository('hana', KEY)))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {'BUILD_TARGETS_STATE_DIR': str(self.account_dir.parent)})))
        pid = os.getpid()
        process = process_start.process_start(pid)
        assert isinstance(process, process_start.Started)
        self.assignment = placement.PlacementAssignment('episode', 'attempt',
            placement.PlacementDestination('ssh', 'hana-gpu', 'hana-gpu', claim_state.RepositoryScope('hana', KEY), 1),
            execution_identity.SourceKeyKnown('source'), placement.SubmittingOwner(store.host_name(), pid, process.start),
            OwningSession('fixture-session'), 0)

    def candidate(self, *, command: tuple[str, ...] = command) -> placement.PlacementCandidate:
        from scripts.direct.step_selection import Pinned

        constraints = build_targets.fixed_placement_constraints(
            self.root, self.package, 'test', command, self.assignment.session, self.assignment.source_key,
            selection=Pinned('hana-gpu'))
        return next(candidate for candidate in constraints.candidates if candidate.destination.target == 'hana-gpu')

    def test_gpu_step_refuses_a_foreign_repository_before_selection(self) -> None:
        """GPU gpu_step row 2."""
        from scripts.build_targets.execution_transport import HanaGpuNextestTransport

        transport = HanaGpuNextestTransport(self.gpu_settings, 'hana-gpu')
        settings = conf.read(self.gpu_settings)
        with (mock.patch.object(conf.ConfigRead, 'words', autospec=True) as selections,
              mock.patch.object(adapter, 'gpu_setup', wraps=adapter.gpu_setup) as setup):
            result = adapter.gpu_step(transport, 'foreign-key', self.package, 'test', self.command,
                                      configuration=settings)
        self.assertEqual(result, placement.FixedExclusion('This target requires the Hana repository'))
        selections.assert_not_called()
        setup.assert_not_called()

    def test_gpu_setup_checks_the_switch_before_saved_grants(self) -> None:
        """GPU gpu_setup row 2."""
        from scripts.build_targets.execution_transport import HanaGpuNextestTransport

        text_setting(self.gpu_settings, 'enabled', 'off')
        transport = HanaGpuNextestTransport(self.gpu_settings, 'hana-gpu')
        with (mock.patch.object(target_config, 'ssh_target_state_directory',
                               wraps=target_config.ssh_target_state_directory) as paths,
              mock.patch.object(claim_state, 'read_json_object', wraps=claim_state.read_json_object) as grants):
            result = adapter.gpu_setup(transport)
        self.assertIsInstance(result, hana.HanaStepRefused)
        paths.assert_not_called()
        grants.assert_not_called()

    def test_empty_gpu_command_is_fixed_before_the_claim_scan(self) -> None:
        """GPU read_gpu_evidence row 2."""
        from scripts.build_targets.execution_transport import HanaGpuNextestTransport

        dest = self.assignment.destination
        transport = HanaGpuNextestTransport(self.gpu_settings, dest.host)
        key = placement.HostEvidenceKey(dest, self.assignment.session, (), 'fixture',
                                        package=self.package, command_kind='test')
        constraints = placement.PlacementConstraints(self.assignment.source_key, (), str(self.root),
            self.package, self.assignment.session, (placement.PlacementCandidate(dest),),
            placement.PinnedHost(dest.target), command_kind='test')
        request = wire.PlacementRequest(self.assignment.episode, self.assignment.owner, 0,
            records.StepProfile(self.package, 'nextest', 'source', records.ExpectedPeak(1024, 'fallback', 0)),
            constraints)
        calls = mock.Mock()
        with (mock.patch.object(placement, 'FixedExclusion', wraps=placement.FixedExclusion) as fixed,
              mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock,
              mock.patch.object(mac_test, 'blocking_claim_block', wraps=mac_test.blocking_claim_block) as block):
            calls.attach_mock(fixed, 'exclude')
            calls.attach_mock(lock, 'lock')
            result = adapter.read_gpu_evidence(transport, key, request)
        self.assertEqual(result.eligibility, placement.FixedExclusion(adapter.GPU_SELECTION_REQUIRED))
        self.assertEqual(calls.mock_calls, [('exclude', (adapter.GPU_SELECTION_REQUIRED,), {}),
                                           ('lock', (claim_state.state_paths(dest.target).lock,), {})])
        block.assert_not_called()

    def test_wrong_gpu_pin_is_fixed_before_the_claim_scan(self) -> None:
        """GPU read_gpu_evidence row 3."""
        from scripts.build_targets.execution_transport import HanaGpuNextestTransport

        dest = self.assignment.destination
        transport = HanaGpuNextestTransport(self.gpu_settings, dest.host)
        key = placement.HostEvidenceKey(dest, self.assignment.session, self.command, 'fixture',
                                        package=self.package, command_kind='test')
        constraints = placement.PlacementConstraints(self.assignment.source_key, self.command, str(self.root),
            self.package, self.assignment.session, (placement.PlacementCandidate(dest),),
            placement.PinnedHost('other-target'), command_kind='test')
        request = wire.PlacementRequest(self.assignment.episode, self.assignment.owner, 0,
            records.StepProfile(self.package, 'nextest', 'source', records.ExpectedPeak(1024, 'fallback', 0)),
            constraints)
        calls = mock.Mock()
        reason = 'This target requires --target hana-gpu'
        with (mock.patch.object(placement, 'FixedExclusion', wraps=placement.FixedExclusion) as fixed,
              mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock,
              mock.patch.object(mac_test, 'blocking_claim_block', wraps=mac_test.blocking_claim_block) as block):
            calls.attach_mock(fixed, 'exclude')
            calls.attach_mock(lock, 'lock')
            result = adapter.read_gpu_evidence(transport, key, request)
        self.assertEqual(result.eligibility, placement.FixedExclusion(reason))
        self.assertEqual(calls.mock_calls, [('exclude', (reason,), {}),
                                           ('lock', (claim_state.state_paths(dest.target).lock,), {})])
        block.assert_not_called()

    def test_ssh_gpu_routing_and_pin_refuse_before_repository_lookup(self) -> None:
        """SSH dispatch row 4."""
        from scripts.aws_target import controller_lock
        from scripts.build_targets import targets
        from scripts.direct.step_selection import Pinned

        target = target_config.configured_ssh_target('hana-gpu')
        with (mock.patch.object(eligibility, 'repository_identity') as identity,
              mock.patch.object(adapter, 'gpu_setup', wraps=adapter.gpu_setup) as setup,
              mock.patch.object(box, 'hana_work_lock', wraps=controller_lock.hana_work_lock) as lock):
            local = build_targets.ssh_target_eligibility(target, self.root, self.package, 'test',
                targets.LocalRequired('fixture requires Linux'), selection=Pinned(target.name))
            unpinned = build_targets.ssh_target_eligibility(
                target, self.root, self.package, 'test', targets.ANY_ELIGIBLE_TARGET)
        self.assertEqual(local, targets.ExcludedTarget(target, 'fixture requires Linux'))
        self.assertEqual(unpinned, targets.ExcludedTarget(target, 'This target requires --target hana-gpu'))
        identity.assert_not_called()
        setup.assert_not_called()
        lock.assert_not_called()

    def test_ssh_gpu_uses_repository_identity_then_shares_the_settings(self) -> None:
        """SSH dispatch row 5."""
        from scripts.aws_target import controller_lock
        from scripts.build_targets import targets
        from scripts.direct.step_selection import Pinned

        target = target_config.configured_ssh_target('hana-gpu')
        settings = conf.read(target.settings)
        calls = mock.Mock()
        with (mock.patch.object(eligibility, 'repository_identity',
                                return_value=eligibility.KnownRepository('hana', KEY)) as identity,
              mock.patch.object(adapter, 'gpu_step', wraps=adapter.gpu_step) as step,
              mock.patch.object(box, 'hana_work_lock', wraps=controller_lock.hana_work_lock) as lock):
            calls.attach_mock(identity, 'identity')
            calls.attach_mock(step, 'step')
            result = build_targets.ssh_target_eligibility(target, self.root, self.package, 'test',
                targets.ANY_ELIGIBLE_TARGET, selection=Pinned(target.name), configuration=settings)
        self.assertEqual(result, targets.EligibleTarget(target))
        self.assertEqual([call[0] for call in calls.mock_calls], ['identity', 'step'])
        self.assertEqual(step.call_args.args[1:], (KEY, self.package, 'test', ()))
        self.assertIs(cast(object, step.call_args.kwargs['configuration']), settings)
        lock.assert_not_called()

    def test_ssh_gpu_returns_fixed_setup_refusal_without_lifecycle_inspection(self) -> None:
        """SSH dispatch row 6."""
        from scripts.aws_target import controller_lock
        from scripts.build_targets import targets
        from scripts.direct.step_selection import Pinned

        target = target_config.configured_ssh_target('hana-gpu')
        self.grants.unlink()
        with (mock.patch.object(adapter, 'gpu_setup', wraps=adapter.gpu_setup) as setup,
              mock.patch.object(hana, 'controller_for', wraps=hana.controller_for) as controller,
              mock.patch.object(box, 'hana_work_lock', wraps=controller_lock.hana_work_lock) as lock):
            result = build_targets.ssh_target_eligibility(target, self.root, self.package, 'test',
                targets.ANY_ELIGIBLE_TARGET, selection=Pinned(target.name))
        self.assertEqual(result, targets.ExcludedTarget(target, adapter.GPU_SETUP_REQUIRED))
        setup.assert_called_once()
        controller.assert_not_called()
        lock.assert_not_called()

    def test_gpu_changed_source_refuses_before_capture_and_target_lock(self) -> None:
        """GPU run_gpu_assignment row 7."""
        from contextlib import redirect_stdout
        from scripts.build_queue import admission_client
        from scripts.buildlog import treekey
        from scripts.copies import copies

        destination = claim_state.state_paths('hana-gpu')
        output = self.root / 'outcome'
        with (mock.patch.object(treekey, 'tree_key', return_value='changed') as key,
              mock.patch.object(copies, 'capture_source', wraps=copies.capture_source) as capture,
              mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock,
              mock.patch.object(admission_client, 'report_execution', side_effect=wire.AdmissionUnavailable('fixture')),
              redirect_stdout(StringIO()), redirect_stderr(StringIO())):
            _ = adapter.run_gpu_assignment(self.assignment, self.root, self.package, self.command, output)
        key.assert_called_once_with(str(self.root))
        capture.assert_not_called()
        lock.assert_not_called()
        self.assertFalse(destination.lock.exists())
        self.assertIn('reason=source_changed', output.read_text())

    def test_gpu_captures_a_commit_before_rechecking_the_caller_source(self) -> None:
        """GPU run_gpu_assignment row 8."""
        from contextlib import redirect_stdout
        from scripts.build_queue import admission_client
        from scripts.buildlog import treekey
        from scripts.copies import copies

        destination = claim_state.state_paths('hana-gpu')
        output = self.root / 'outcome'
        from scripts.lint.memory_test_support import write_config_fixture

        repo = self.root / 'source'
        repo.mkdir()
        _ = subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        _ = (repo / 'fixture').write_text('before\n')
        _ = subprocess.run(['git', '-C', str(repo), 'add', 'fixture'], check=True)
        _ = subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture',
            '-c', 'user.email=fixture@example.invalid', '-c', 'core.hooksPath=/dev/null',
            'commit', '-qm', 'Fixture'], check=True)
        _ = write_config_fixture(self.root / 'copies.conf', template='copies')
        _ = (repo / 'fixture').write_text('captured\n')
        key = treekey.tree_key(str(repo))
        assert key is not None
        assignment = replace(self.assignment, source_key=execution_identity.SourceKeyKnown(key))
        captured: list[str] = []
        real_capture = copies.capture_source

        def capture(source: Path) -> str:
            self.assertFalse(destination.lock.exists())
            commit = real_capture(source)
            captured.append(commit)
            _ = (repo / 'fixture').write_text('after capture\n')
            return commit

        with (mock.patch.object(copies, 'capture_source', side_effect=capture) as save,
              mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock,
              mock.patch.object(admission_client, 'report_execution', side_effect=wire.AdmissionUnavailable('fixture')),
              redirect_stdout(StringIO()), redirect_stderr(StringIO())):
            _ = adapter.run_gpu_assignment(assignment, repo, self.package, self.command, output)
        save.assert_called_once_with(repo)
        commit, = captured
        saved = subprocess.run(['git', '-C', str(repo), 'show', f'{commit}:fixture'],
                              check=True, capture_output=True, text=True)
        self.assertEqual(saved.stdout, 'captured\n')
        self.assertIn('reason=source_changed', output.read_text())
        lock.assert_not_called()

    def test_gpu_rechecks_source_after_capture_before_taking_target_lock(self) -> None:
        """GPU run_gpu_assignment row 9."""
        from contextlib import redirect_stdout
        from scripts.build_queue import admission_client
        from scripts.buildlog import treekey
        from scripts.copies import copies

        destination = claim_state.state_paths('hana-gpu')
        output = self.root / 'outcome'
        trace = mock.Mock()
        with (mock.patch.object(treekey, 'tree_key', side_effect=['source', 'changed']) as key,
              mock.patch.object(copies, 'capture_source', return_value='captured-commit') as capture,
              mock.patch.object(claim_state, 'locked', wraps=claim_state.locked) as lock,
              mock.patch.object(admission_client, 'report_execution', side_effect=wire.AdmissionUnavailable('fixture')),
              redirect_stdout(StringIO()), redirect_stderr(StringIO())):
            trace.attach_mock(key, 'key')
            trace.attach_mock(capture, 'capture')
            _ = adapter.run_gpu_assignment(self.assignment, self.root, self.package, self.command, output)
        self.assertEqual([call[0] for call in trace.mock_calls], ['key', 'capture', 'key'])
        lock.assert_not_called()
        self.assertFalse(destination.lock.exists())
        self.assertIn('reason=source_changed', output.read_text())

    def test_assigned_gpu_trial_rejects_link_before_root_lock(self) -> None:
        """GPU assigned trial handoff row 1."""
        from scripts.aws_target import controller_lock, gpu_size_trial as trial

        self.trials.mkdir()
        destination = self.root / 'outside'
        destination.mkdir()
        link = self.trials / 'linked'
        link.symlink_to(destination, target_is_directory=True)
        launch = trial.AssignedHanaGpuLaunch(link.name, lambda: self.fail('The trial started'))
        request = trial.HanaGpuNextestRequest(('--workspace', '--test', 'fixture'), ('g6.2xlarge',))
        with (mock.patch.object(trial, 'trial_root', return_value=self.trials),
              mock.patch.object(trial, 'control_lock', wraps=controller_lock.control_lock) as root_lock,
              self.assertRaisesRegex(ValueError, 'canonical direct child')):
            _ = trial.run_nextest(self.root, 'HEAD', request, launch=launch)
        root_lock.assert_not_called()
        self.assertEqual(list(destination.iterdir()), [])

    def test_gpu_preparation_worker_runs_under_targeted_gate_reservation(self) -> None:
        """GPU assigned trial handoff row 5."""
        from scripts.aws_target import gpu_size_trial as trial
        from scripts.lint.memory_test_support import write_config_fixture
        from scripts.production import gate
        from scripts.production.gate_modules import GateRefused

        folder = self.trials / 'fixture-trial'
        launch = trial.AssignedHanaGpuLaunch(folder.name, lambda: self.fail('The service started'))
        request = trial.HanaGpuNextestRequest(('--workspace', '--test', 'fixture'), ('g6.2xlarge',))
        _ = write_config_fixture(self.gpu_settings,
            'hana_gpu_spot_max_usd_per_hour={"g6.2xlarge":0.75}\n'
            + 'gpu_size_trial_limit_usd=2\nhana_gpu_lifetime_minutes=10\ngpu_size_trial_box_extra_usd=0.10\n',
            template='cloud_gate')
        trace = mock.Mock()
        with (mock.patch.object(trial, 'trial_root', return_value=self.trials),
              mock.patch.object(trial, 'checked', return_value='a' * 40),
              mock.patch.object(trial, 'export_source', return_value=self.root),
              mock.patch.object(trial, 'preflight', return_value={}),
              mock.patch.object(gate.BuildHoldWait, 'wait_until_free') as wait,
              mock.patch.object(gate, 'gate_reservation') as reserve,
              mock.patch.object(trial, 'start_service') as service):
            held = cast(mock.MagicMock, reserve.return_value)
            enter = cast(mock.MagicMock, held.__enter__)
            place = cast(mock.MagicMock, enter.return_value)
            popen = cast(mock.MagicMock, place.popen)
            worker = cast(mock.MagicMock, popen.return_value)
            exited = cast(mock.MagicMock, worker.wait)
            release = cast(mock.MagicMock, held.__exit__)
            exited.return_value = 7
            for name, spy in (('wait', wait), ('reserve', reserve), ('enter', enter), ('popen', popen),
                              ('exit_status', exited), ('release', release)):
                trace.attach_mock(spy, name)
            with self.assertRaisesRegex(GateRefused, 'preparation failed with exit 7'):
                _ = trial.run_nextest(self.root, 'HEAD', request, launch=launch)
        self.assertEqual([call[0] for call in trace.mock_calls],
                         ['wait', 'reserve', 'enter', 'wait', 'popen', 'exit_status', 'release'])
        self.assertEqual(reserve.call_args.kwargs['mode'], gate.SuiteMode.TARGETED)
        self.assertEqual(cast(list[str], popen.call_args.args[0])[-2:], ['--prepare-worker', str(folder)])
        self.assertIn('preparation failed with exit 7', (folder / 'prepare.log').read_text())
        service.assert_not_called()

    def test_assigned_service_reads_account_paths_while_trial_lease_is_held(self) -> None:
        """GPU assigned trial handoff row 6."""
        from scripts.aws_target import cloud_gate, gpu_size_trial as trial

        folder = self.trials / 'fixture-trial'
        request = trial.HanaGpuNextestRequest(('--workspace', '--test', 'fixture'), ('g6.2xlarge',))
        def prepare(_repo: Path, _commit: str, prepared: Path, mode: trial.HanaGpuNextestRequest) -> None:
            trial.write_json(prepared / 'manifest.json', {
                'schema': 2, 'toolchain_source': 'nix', 'state': 'prepared', 'trial': prepared.name,
                'commit': 'a' * 40, 'tree': 'b' * 40, 'sha256': {},
                'hana_nextest': {'arguments': mode.arguments, 'instance_types': mode.instance_types},
            })
        launch = trial.AssignedHanaGpuLaunch(folder.name, lambda: None)
        trace = mock.Mock()

        def worker(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            with self.assertRaises(BlockingIOError), trial.trial_lock(folder, exclusive=True):
                self.fail('The trial lease was free during service startup')
            self.assertIn(str(self.settings), arguments)
            self.assertIn(str(self.account_dir), arguments)
            return subprocess.CompletedProcess(arguments, 7)

        def command(_module: str, args: Sequence[str], _unit: str) -> list[str]:
            return ['fixture', *args]

        with (mock.patch.object(trial, 'trial_root', return_value=self.trials),
              mock.patch.object(trial, 'prepare_locked', side_effect=prepare),
              mock.patch.object(trial, 'held_trial', wraps=trial.held_trial) as lease,
              mock.patch.object(trial, 'ssh_target_state_directory', return_value=self.account_dir),
              mock.patch.object(conf, 'path', wraps=conf.path) as settings,
              mock.patch.object(cloud_gate, 'service_command', side_effect=command),
              mock.patch.object(subprocess, 'run', side_effect=worker) as execute,
              self.assertRaisesRegex(ValueError, 'service did not start')):
            trace.attach_mock(lease, 'lease')
            trace.attach_mock(execute, 'execute')
            _ = trial.run_nextest(self.root, 'HEAD', request, launch=launch)
        self.assertIn(mock.call('aws_target'), settings.call_args_list)
        self.assertEqual([call[0] for call in trace.mock_calls], ['lease', 'execute'])

    def test_assigned_service_validates_trial_identity_before_launch(self) -> None:
        """GPU assigned trial handoff row 7."""
        from scripts.aws_target import cloud_gate, gpu_size_trial as trial

        folder = self.trials / 'fixture-trial'
        request = trial.HanaGpuNextestRequest(('--workspace', '--test', 'fixture'), ('g6.2xlarge',))
        def prepare(_repo: Path, _commit: str, prepared: Path, mode: trial.HanaGpuNextestRequest) -> None:
            trial.write_json(prepared / 'manifest.json', {
                'schema': 2, 'toolchain_source': 'nix', 'state': 'prepared', 'trial': 'other-trial',
                'commit': 'a' * 40, 'tree': 'b' * 40, 'sha256': {},
                'hana_nextest': {'arguments': mode.arguments, 'instance_types': mode.instance_types},
            })
        launch = trial.AssignedHanaGpuLaunch(folder.name, lambda: self.fail('The callback ran'))
        with (mock.patch.object(trial, 'trial_root', return_value=self.trials),
              mock.patch.object(trial, 'prepare_locked', side_effect=prepare),
              mock.patch.object(trial, 'validate_prepared', wraps=trial.validate_prepared) as validate,
              mock.patch.object(cloud_gate, 'service_command') as command,
              mock.patch.object(subprocess, 'run') as worker,
              self.assertRaisesRegex(ValueError, 'does not match its assigned trial')):
            _ = trial.run_nextest(self.root, 'HEAD', request, launch=launch)
        validate.assert_called_once_with(folder)
        command.assert_not_called()
        worker.assert_not_called()

    def test_assigned_service_callback_runs_before_the_worker_command(self) -> None:
        """GPU assigned trial handoff row 8."""
        from scripts.aws_target import cloud_gate, gpu_size_trial as trial

        folder = self.trials / 'fixture-trial'
        request = trial.HanaGpuNextestRequest(('--workspace', '--test', 'fixture'), ('g6.2xlarge',))
        def prepare(_repo: Path, _commit: str, prepared: Path, mode: trial.HanaGpuNextestRequest) -> None:
            trial.write_json(prepared / 'manifest.json', {
                'schema': 2, 'toolchain_source': 'nix', 'state': 'prepared', 'trial': prepared.name,
                'commit': 'a' * 40, 'tree': 'b' * 40, 'sha256': {},
                'hana_nextest': {'arguments': mode.arguments, 'instance_types': mode.instance_types},
            })
        callback = mock.Mock()
        launch = trial.AssignedHanaGpuLaunch(folder.name, callback)
        trace = mock.Mock()
        def command(_module: str, args: Sequence[str], _unit: str) -> list[str]:
            return ['fixture', *args]

        with (mock.patch.object(trial, 'trial_root', return_value=self.trials),
              mock.patch.object(trial, 'prepare_locked', side_effect=prepare),
              mock.patch.object(cloud_gate, 'service_command', side_effect=command),
              mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 7)) as worker,
              self.assertRaisesRegex(ValueError, 'service did not start')):
            trace.attach_mock(callback, 'check')
            trace.attach_mock(worker, 'worker')
            _ = trial.run_nextest(self.root, 'HEAD', request, launch=launch)
        self.assertEqual([call[0] for call in trace.mock_calls], ['check', 'worker'])
        arguments = cast(list[str], worker.call_args.args[0])
        self.assertEqual(arguments[arguments.index('--worker') + 1], str(folder))
        self.assertIn('--assigned', arguments)

    def test_unstarted_service_validates_manifest_before_input_cleanup(self) -> None:
        """GPU assigned trial handoff row 10."""
        from scripts.aws_target import cloud_gate, gpu_size_trial as trial

        folder = self.trials / 'fixture-trial'
        request = trial.HanaGpuNextestRequest(('--workspace', '--test', 'fixture'), ('g6.2xlarge',))
        def prepare(_repo: Path, _commit: str, prepared: Path, mode: trial.HanaGpuNextestRequest) -> None:
            trial.write_json(prepared / 'manifest.json', {
                'schema': 2, 'toolchain_source': 'nix', 'state': 'prepared', 'trial': prepared.name,
                'commit': 'a' * 40, 'tree': 'b' * 40, 'sha256': {},
                'hana_nextest': {'arguments': mode.arguments, 'instance_types': mode.instance_types},
            })
        launch = trial.AssignedHanaGpuLaunch(folder.name, lambda: None)
        trace = mock.Mock()
        def command(_module: str, args: Sequence[str], _unit: str) -> list[str]:
            return ['fixture', *args]

        with (mock.patch.object(trial, 'trial_root', return_value=self.trials),
              mock.patch.object(trial, 'prepare_locked', side_effect=prepare),
              mock.patch.object(cloud_gate, 'service_command', side_effect=command),
              mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 7)) as worker,
              mock.patch.object(trial, 'validate_prepared', wraps=trial.validate_prepared) as validate,
              mock.patch.object(trial, 'remove_run_inputs', wraps=trial.remove_run_inputs) as cleanup,
              self.assertRaisesRegex(ValueError, 'service did not start \\(status 7\\)')):
            for name, spy in (('validate', validate), ('worker', worker), ('cleanup', cleanup)):
                trace.attach_mock(spy, name)
            _ = trial.run_nextest(self.root, 'HEAD', request, launch=launch)
        self.assertEqual([call[0] for call in trace.mock_calls], ['validate', 'worker', 'validate', 'cleanup'])
        self.assertEqual(validate.call_args_list[-1], mock.call(folder, allow_removed_before_upload=True))
        self.assertTrue((folder / 'inputs-removed.json').is_file())

    def test_assigned_trial_reads_exact_result_receipt_manifest_and_attempt(self) -> None:
        """GPU assigned trial handoff row 11."""
        from scripts.aws_target import cloud_gate, gpu_size_trial as trial

        folder = self.trials / ('f' * 32)
        request = trial.HanaGpuNextestRequest(('--workspace', '--test', 'fixture'), ('g6.2xlarge',))
        def prepare(_repo: Path, _commit: str, prepared: Path, mode: trial.HanaGpuNextestRequest) -> None:
            trial.write_json(prepared / 'manifest.json', {
                'schema': 2, 'toolchain_source': 'nix', 'state': 'prepared', 'trial': prepared.name,
                'commit': 'a' * 40, 'tree': 'b' * 40, 'sha256': {},
                'hana_nextest': {'arguments': mode.arguments, 'instance_types': mode.instance_types},
            })
        launch = trial.AssignedHanaGpuLaunch(folder.name, lambda: None)
        size = request.instance_types[0]
        attempt = trial.gpu_attempt_id(folder.name, size)
        finished = trial.gpu_box(folder, folder.name, size, attempt) / 'finished.json'

        def worker(_arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            receipt = trial.cleanup_receipt(folder)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            trial.write_json(receipt, {'trial': folder.name})
            trial.write_json(folder / 'online-result.json', {'status': 'complete', 'instance_type': size, 'attempt': attempt})
            finished.parent.mkdir(parents=True)
            trial.write_json(finished, {'status': 'complete', 'nextest': {
                'attempt': attempt, 'commit': 'a' * 40, 'tree': 'b' * 40,
                'exit_code': 2, 'stdout': 'fixture output', 'stderr': 'fixture error',
            }})
            return subprocess.CompletedProcess([], 2)

        def command(_module: str, args: Sequence[str], _unit: str) -> list[str]:
            return ['fixture', *args]

        with (mock.patch.object(trial, 'trial_root', return_value=self.trials),
              mock.patch.object(trial, 'prepare_locked', side_effect=prepare),
              mock.patch.object(cloud_gate, 'service_command', side_effect=command),
              mock.patch.object(subprocess, 'run', side_effect=worker),
              mock.patch.object(trial, 'read_json', wraps=trial.read_json) as read):
            result = trial.run_nextest(self.root, 'HEAD', request, launch=launch)
        self.assertEqual(result, trial.HanaGpuNextestResult(
            attempt, 'a' * 40, 'b' * 40, 2, 'fixture output', 'fixture error'))
        self.assertEqual(read.call_args_list[-3:], [
            mock.call(folder / 'online-result.json'), mock.call(folder / 'manifest.json'), mock.call(finished)])

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
        _ = self.gpu_settings.write_text(self.gpu_settings.read_text().replace('hana_gpu_route_enabled=on', 'hana_gpu_route_enabled=off'))
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

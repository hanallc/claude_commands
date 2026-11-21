"""Exercise lifecycle recovery with private journals and one fake provider."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import dataclass, field, replace
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import cast, override
from unittest import mock

from scripts.aws_target import admin_secrets, aws_target, deploy, hana_box, hana_placement, hana_provider, hana_render, hana_replacement_record, hana_spend, ledger, hana_execution
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.ledger_test_support import CostFixture, HANA_GUEST_RUNTIME_SETTINGS, git_only, text_setting
from scripts.lib import conf, process_start
from scripts.build_queue import placement
from scripts.mac_test.claim_state import RepositoryScope


IDENTITY = hana_box.DeploymentIdentity('fixture-deployment', 'fixture-instance', 'fixture-spot-request', 'us-east-2')


def write_lifecycle_configuration(path: Path) -> None:
    """Give lifecycle tests fixed limits in their private configuration."""
    _ = path.write_text("""\
enabled=on
host=fixture-hana
region=us-east-2
instance_type=m7a.2xlarge
fallback_instance_types=c7a.4xlarge,m7i.4xlarge,m6a.4xlarge
spot_max_usd_per_hour=0.15
daily_budget_usd=6
gp3_usd_per_gib_month=0.08
# whole: 1..
root_gib=40
# whole: 1..
data_gib=300
# whole: 1..
idle_stop_seconds=600
# whole: 1..
start_timeout_seconds=120
# whole: 1..
tick_interval_s=5
# whole: 1..
controller_tick_timeout_s=45
# whole: 1..
guard_grace_seconds=60
# whole: 1..
watchdog_check_seconds=1
# whole: 1..
permit_max_seconds=1800
# whole: 1..
step_cpu_limit=4
# whole: 1..
step_memory_gib=24
# whole: 1..
mount_timeout_seconds=15
# whole: 1..
aws_timeout_s=10
# whole: 1..
result_transfer_timeout_s=83
# whole: 1..
control_url_seconds=3600
""" + HANA_GUEST_RUNTIME_SETTINGS)


def record_lifecycle_image(directory: Path, workload: Path, *, identity: hana_box.DeploymentIdentity = IDENTITY) -> Path:
    path = directory / 'hana/setup.json'
    path.parent.mkdir(exist_ok=True, parents=True)
    _ = path.write_text(json.dumps({'deployment': identity.deployment, 'instance': identity.instance,
        'guest_digest': hana_render.guest_digest(workload).value}))
    return path


@dataclass
class FakeProvider:
    directory: Path
    clock: TestClock
    state: ledger.InstanceState = 'stopped'
    boot: hana_box.VerifiedBoot | hana_box.BootUnverified = hana_box.BootUnverified()
    cause: hana_box.StopCause = hana_box.StopCause.UNKNOWN
    calls: list[hana_box.PendingOperation] = field(default_factory=list)
    starts: set[str] = field(default_factory=set)
    in_flight: set[str] = field(default_factory=set)
    crash_after_start: bool = False
    bad_ack: bool = False
    crash_after_mutations: set[hana_box.Action] = field(default_factory=set)
    recoveries: list[hana_box.PendingOperation] = field(default_factory=list)
    published: list[hana_box.PendingOperation] = field(default_factory=list)
    during_call: Callable[[], None] = lambda: None
    pending_bootstrap: bool = False
    abandoned: list[str] = field(default_factory=list)

    def check_intent(self, operation: hana_box.PendingOperation) -> None:
        path = self.directory / 'hana/lifecycle.json'
        saved = ledger.mapping(cast(object, json.loads(path.read_text())))
        intent = ledger.mapping(saved['operation'])
        if intent['token'] != operation.token or intent['action'] != operation.action.value:
            raise AssertionError('Persist the operation before its provider call')
        for name in ('controller.lock', 'cost-ledger.lock'):
            with (self.directory / name).open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)

    def step(self, identity: hana_box.DeploymentIdentity, start: hana_box.DemandStart | hana_box.NoDemandStart,
             operation: hana_box.PendingOperation) -> hana_box.ProviderReply:
        self.calls.append(operation)
        self.check_intent(operation)
        self.in_flight.add(operation.token)
        try:
            return self.mutate(identity, start, operation)
        finally:
            self.in_flight.discard(operation.token)

    def mutate(self, identity: hana_box.DeploymentIdentity, start: hana_box.DemandStart | hana_box.NoDemandStart,
               operation: hana_box.PendingOperation) -> hana_box.ProviderReply:
        self.during_call()
        if operation.action == hana_box.Action.OBSERVE:
            return hana_box.BoxObservation(identity, self.clock.now, self.state, self.boot, self.cause)
        if operation.action == hana_box.Action.BOOTSTRAP and self.pending_bootstrap:
            raise hana_provider.ProviderPending('The Hana bootstrap operation remains unresolved')
        if operation.action == hana_box.Action.START:
            assert isinstance(start, hana_box.DemandStart)
            self.starts.add(operation.token)
            self.state, self.boot = 'running', hana_box.VerifiedBoot('fixture-boot-' + start.token, start.token)
            if self.crash_after_start:
                self.crash_after_start = False
                raise RuntimeError('The start response was lost')
        if operation.action == hana_box.Action.STOP:
            self.state = 'stopped'
        if operation.action == hana_box.Action.PUBLISH:
            self.published.append(operation)
        if operation.action in self.crash_after_mutations:
            self.crash_after_mutations.remove(operation.action)
            raise RuntimeError('The mutation response was lost')
        if operation.action == hana_box.Action.PUBLISH:
            assert isinstance(self.boot, hana_box.VerifiedBoot)
            return hana_box.PermitAccepted(operation.token, identity, self.boot,
                operation.generation + int(self.bad_ack), operation.execution)
        return hana_box.OperationAccepted(operation.token)

    def recover(self, identity: hana_box.DeploymentIdentity, start: hana_box.DemandStart | hana_box.NoDemandStart,
                operation: hana_box.PendingOperation, *, abandon_start: bool = False) -> hana_box.MutationRecovery:
        _ = start
        self.recoveries.append(operation)
        self.check_intent(operation)
        observed = hana_box.BoxObservation(identity, self.clock.now, self.state, self.boot, self.cause)
        if operation.token in self.in_flight:
            return hana_box.MutationUnresolved(operation.token, observed)
        if abandon_start:
            self.abandoned.append(operation.token)
        return hana_box.MutationSettled(operation.token, observed)


class HanaBoxTests(CostFixture):
    clock: TestClock = TestClock()
    workload_path: Path = Path()
    provider: FakeProvider
    controller: hana_box.HanaController
    book: ledger.CostLedger = ledger.CostLedger(Path())

    def __init__(self, methodName: str = 'runTest'):
        super().__init__(methodName)
        self.provider = FakeProvider(Path(), TestClock())
        self.controller = hana_box.HanaController(Path(), Path(), Path(), self.provider)

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(mock.patch.object(subprocess, 'run', side_effect=git_only))
        self.clock = TestClock()
        self.workload_path = self.root / 'hana_aws.conf'
        write_lifecycle_configuration(self.workload_path)
        self.book = ledger.CostLedger(self.state)
        self.book.record_inventory(ledger.ResourceInventory(self.clock.now, (), 'private inventory', True))
        self.book.observe(ledger.InstanceObservation(self.clock.now, 'terminated', 'private account inventory'))
        self.provider = FakeProvider(self.state, self.clock)
        self.controller = hana_box.HanaController(self.state, self.settings, self.workload_path, self.provider, clock=self.clock)
        self.controller.register(IDENTITY)
        _ = record_lifecycle_image(self.state, self.workload_path)

    def test_only_a_current_image_can_start_a_hana_claim(self) -> None:
        image = self.state / 'hana/setup.json'
        current = image.read_text()
        for proof in ('{}', current.replace(IDENTITY.instance, 'another-box')):
            with self.subTest(proof=proof):
                _ = image.write_text(proof)
                self.assertEqual(self.controller.request('execution-one'), hana_box.ClaimRefused(hana_box.HANA_IMAGE_STALE))
        _ = image.write_text(current)
        for key, value in (('spot_max_usd_per_hour', '0.20'), ('execution_shell', 'ci')):
            with self.subTest(key=key):
                text_setting(self.workload_path, key, value)
                self.assertTrue(self.controller.image_is_current(IDENTITY))
        self.assertIsInstance(self.controller.request('execution-one'), hana_box.ClaimCommitted)
        _ = self.workload_path.write_text(self.workload_path.read_text().replace(
            'watchdog_check_seconds=1', 'watchdog_check_seconds=2'))
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.controller.claim_status('execution-one'), hana_box.ClaimRefused(hana_box.HANA_IMAGE_STALE))

    def record(self) -> dict[str, object]:
        return ledger.mapping(cast(object, json.loads((self.state / 'hana/lifecycle.json').read_text())))

    def generation(self) -> int:
        return max((row.generation for row in self.book.read_snapshot().hana_compute), default=0)

    def tick(self) -> hana_box.Lifecycle:
        self.clock.advance(1)
        result = self.controller.tick()
        assert isinstance(result, hana_box.Lifecycle)
        return result

    def ready(self) -> None:
        self.assertIsInstance(self.controller.request('execution-one'), hana_box.ClaimCommitted)
        for _step in range(4):
            _ = self.tick()
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.READY.value)

    def test_start_reserves_before_bootstrap_and_grants_only_verified_claim_permission(self) -> None:
        self.assertIsInstance(self.controller.request('execution-one'), hana_box.ClaimCommitted)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STARTING)
        exposure = self.book.read_snapshot().hana_compute[-1]
        self.assertGreater(exposure.expires, self.clock.now)
        self.assertEqual(self.provider.calls[0].action, hana_box.Action.BOOTSTRAP)
        self.assertIsInstance(self.controller.permission('execution-one', 'unverified', exposure.generation), hana_box.ClaimRefused)
        for _step in range(3):
            _ = self.tick()
        assert isinstance(self.provider.boot, hana_box.VerifiedBoot)
        self.assertIsInstance(self.controller.permission('execution-one', self.provider.boot.boot_id, self.generation()),
                              hana_box.CommandPermission)
        self.assertEqual(len(self.provider.starts), 1)

    def test_assigned_execution_guard_keeps_a_ready_claim_until_durable_settlement(self) -> None:

        self.ready()
        self.controller.bind_execution('execution-one')
        self.assertIsInstance(self.controller.release('execution-one'), hana_placement.HanaClaimReleaseRefused)
        reference = placement.ProviderClaimReference('ssh', 'fixture-target', RepositoryScope('hana', 'fixture-repository', 1),
            'execution-one', 'fixture-episode', 'fixture-attempt')
        settled = hana_execution.settle_unpublished_claim(self.state, reference,
            release_claim=lambda: self.controller.release('execution-one'), reason='The capture was refused')
        self.assertIsInstance(settled, hana_execution.HanaSettled)
        self.assertEqual(self.record()['claims'], [])
        self.assertIsInstance(self.controller.release('execution-one'), hana_placement.HanaClaimReleased)

    def test_missing_or_corrupt_execution_journal_keeps_the_exact_claim_after_restart(self) -> None:
        self.ready()
        self.controller.bind_execution('execution-one')
        restarted = hana_box.HanaController(self.state, self.settings, self.workload_path, self.provider, clock=self.clock)
        journal = hana_execution.execution_path(self.state, 'execution-one')
        for state in ('missing', 'corrupt'):
            with self.subTest(state=state):
                if state == 'corrupt':
                    _ = journal.write_text('{')
                refusal = restarted.release('execution-one')
                assert isinstance(refusal, hana_placement.HanaClaimReleaseRefused)
                self.assertEqual(refusal.reason, 'The Hana command settlement is unconfirmed')

    def test_execution_bound_checks_lifecycle_under_the_controller_lock(self) -> None:
        def read_locked() -> hana_box.NoDeployment:
            with (self.state / 'controller.lock').open('a') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return hana_box.NoDeployment()

        with mock.patch.object(self.controller, '_read', side_effect=read_locked):
            self.assertFalse(self.controller.execution_bound('never-bound'))
        self.assertFalse(self.controller.execution_bound('never-bound'))
        self.assertEqual(self.provider.calls, [])

    def test_execution_bound_retains_a_guard_with_missing_or_corrupt_journal(self) -> None:
        self.controller.bind_execution('execution-one')
        self.assertTrue(self.controller.execution_bound('execution-one'))
        _ = hana_execution.execution_path(self.state, 'execution-one').write_text('{')
        self.assertTrue(self.controller.execution_bound('execution-one'))

    def test_execution_bound_retains_matching_lifecycle_claims_and_operations(self) -> None:
        _ = self.controller.request('execution-one')
        self.assertTrue(self.controller.execution_bound('execution-one'))
        self.assertFalse(self.controller.execution_bound('another-execution'))
        row = self.record()
        row['claims'] = []
        row['operation'] = {'action': hana_box.Action.START.value, 'token': 'operation-token',
            'at': self.clock.now, 'generation': 1, 'permit': None, 'expected_generation': 0,
            'execution': 'execution-one'}
        _ = self.controller.path.write_text(json.dumps(row))
        self.assertTrue(self.controller.execution_bound('execution-one'))
        self.assertFalse(self.controller.execution_bound('another-execution'))

    def test_execution_bound_retains_unreadable_guard_or_lifecycle_evidence(self) -> None:
        with mock.patch.object(Path, 'stat', side_effect=PermissionError('The guard cannot be read')):
            self.assertTrue(self.controller.execution_bound('execution-one'))
        _ = self.controller.path.write_text('{')
        self.assertTrue(self.controller.execution_bound('execution-one'))

    def test_spending_refusal_makes_no_provider_mutation(self) -> None:
        workload = hana_spend.read_settings(conf.read(self.workload_path))
        self.costs({'daily_limit_usd': str(workload.storage_usd_per_month / 60)})
        self.assertIsInstance(self.controller.request('execution-one'), hana_box.ClaimCommitted)
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.book.read_snapshot().hana_compute, [])
        self.assertEqual(self.controller.claim_status('execution-one'), hana_box.ClaimRefused('daily_limit'))

    def test_expired_waiting_claim_is_refused_without_bootstrap(self) -> None:
        committed = self.controller.request('execution-one')
        assert isinstance(committed, hana_box.ClaimCommitted)
        self.clock.advance(hana_box.read_settings(conf.read(self.workload_path)).start_timeout_seconds)
        self.assertEqual(self.controller.claim_status('execution-one'), hana_box.ClaimWaiting(committed.claim))
        self.assertEqual(self.controller.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertEqual(self.controller.claim_status('execution-one'),
                         hana_box.ClaimRefused('The Hana start deadline expired'))
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.book.read_snapshot().hana_compute, [])

    def test_start_deadline_refuses_claim_before_the_barrier_settles(self) -> None:
        _ = self.controller.request('execution-one')
        _ = self.tick()
        self.clock.advance(hana_box.read_settings(conf.read(self.workload_path)).start_timeout_seconds)
        self.assertEqual(self.controller.claim_status('execution-one'),
                         hana_box.ClaimRefused('The Hana start deadline expired'))
        self.assertIsInstance(self.controller.release('execution-one'), hana_placement.HanaClaimReleaseRefused)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)
        self.assertEqual(len(ledger.rows(self.record(), 'claims')), 1)

    def test_pending_bootstrap_deadline_releases_claim_and_ignores_late_reply(self) -> None:
        self.provider.pending_bootstrap = True
        _ = self.controller.request('execution-one')
        for _step in range(2):
            with self.assertRaises(hana_provider.ProviderPending):
                _ = self.tick()
        operation = self.provider.calls[-1]
        deadline = ledger.instant(ledger.mapping(self.record()['start']), 'deadline')

        def late_reply(_identity: hana_box.DeploymentIdentity, _start: hana_box.DemandStart | hana_box.NoDemandStart,
                       pending: hana_box.PendingOperation) -> hana_box.ProviderReply:
            self.assertEqual(pending, operation)
            self.clock.now = deadline
            self.assertEqual(self.controller.tick(), hana_box.Lifecycle.IDLE_STOPPED)
            return hana_box.OperationAccepted(pending.token)

        with mock.patch.object(self.provider, 'step', side_effect=late_reply):
            self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertEqual(self.provider.abandoned, [operation.token])
        self.assertEqual(self.record()['claims'], [])
        self.assertIsNone(self.record()['operation'])
        self.assertIsNone(self.record()['start'])
        self.assertEqual(self.controller.claim_status('execution-one'),
                         hana_box.ClaimRefused('The Hana start deadline expired'))
        exposure, = self.book.read_snapshot().hana_compute
        self.assertEqual(exposure.state, ledger.HanaExposureClosed(deadline))

    def test_bootstrap_acknowledgment_at_the_deadline_does_not_schedule_start(self) -> None:
        _ = self.controller.request('execution-one')

        def reach_deadline() -> None:
            self.clock.now = ledger.instant(ledger.mapping(self.record()['start']), 'deadline')

        self.provider.during_call = reach_deadline
        self.assertEqual(self.tick(), hana_box.Lifecycle.STARTING)
        self.assertEqual(ledger.mapping(self.record()['operation'])['action'], hana_box.Action.BOOTSTRAP)
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertFalse(self.provider.starts)

    def test_stopped_start_deadline_cancels_before_dispatch(self) -> None:
        _ = self.controller.request('execution-one')
        _ = self.tick()
        operation = ledger.mapping(self.record()['operation'])
        self.assertEqual(operation['action'], hana_box.Action.START)
        self.clock.now = ledger.instant(ledger.mapping(self.record()['start']), 'deadline')
        self.assertEqual(self.controller.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertEqual(self.provider.abandoned, [operation['token']])
        self.assertFalse(self.provider.starts)
        self.assertEqual(self.record()['claims'], [])

    def test_dead_start_owner_releases_the_stopped_box_before_deadline(self) -> None:
        self.provider.pending_bootstrap = True
        _ = self.controller.request('execution-one')
        with self.assertRaises(hana_provider.ProviderPending):
            _ = self.tick()
        with mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()):
            self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertEqual(self.controller.claim_status('execution-one'), hana_box.ClaimRefused('The Hana start owner exited'))
        self.assertEqual(self.record()['claims'], [])
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)

    def test_start_intent_recovers_after_crash_before_response(self) -> None:
        _ = self.controller.request('execution-one')
        _ = self.tick()
        self.provider.crash_after_start = True
        with self.assertRaises(RuntimeError):
            _ = self.tick()
        operation = ledger.mapping(self.record()['operation'])
        self.assertEqual(operation['action'], hana_box.Action.START.value)
        self.controller = hana_box.HanaController(self.state, self.settings, self.workload_path, self.provider, clock=self.clock)
        for _step in range(3):
            _ = self.tick()
        self.assertEqual(len(self.provider.starts), 1)
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.READY.value)
        starts = [row for row in self.provider.calls if row.action == hana_box.Action.START]
        self.assertEqual(starts[0], starts[1])

    def test_cap_refusal_during_start_keeps_the_intent_and_compute_exposure(self) -> None:
        _ = self.controller.request('execution-one')
        _ = self.tick()
        start_intent = ledger.mapping(self.record()['operation'])
        self.assertEqual(start_intent['action'], hana_box.Action.START.value)
        concurrent_intents: list[object] = []

        def refuse_spending_during_start() -> None:
            self.provider.during_call = lambda: None
            workload = hana_spend.read_settings(conf.read(self.workload_path))
            self.costs({'daily_limit_usd': str(workload.storage_usd_per_month / 60)})
            other = hana_box.HanaController(self.state, self.settings, self.workload_path,
                                           self.provider, clock=self.clock)
            for _step in range(2):
                self.clock.advance(1)
                _ = other.tick()
                concurrent_intents.append(self.record()['operation'])

        self.provider.during_call = refuse_spending_during_start
        _ = self.tick()
        self.assertEqual(self.provider.state, 'running')
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureOpen)
        self.assertEqual(concurrent_intents, [start_intent, start_intent])
        self.assertEqual(ledger.mapping(self.record()['operation'])['action'], hana_box.Action.OBSERVE.value)
        self.assertEqual(self.provider.starts, {str(start_intent['token'])})

    def lose_mutation_response(self, action: hana_box.Action) -> None:
        _ = self.controller.request('execution-one')
        preceding = {hana_box.Action.BOOTSTRAP: 0, hana_box.Action.START: 1, hana_box.Action.PUBLISH: 3}
        for _step in range(preceding[action]):
            _ = self.tick()
        self.provider.crash_after_mutations.add(action)
        with self.assertRaisesRegex(RuntimeError, 'The mutation response was lost'):
            _ = self.tick()
        self.assertEqual(ledger.mapping(self.record()['operation'])['action'], action.value)
        self.clock.now = self.book.read_snapshot().hana_compute[-1].expires + 1
        self.controller = hana_box.HanaController(self.state, self.settings, self.workload_path,
                                                self.provider, clock=self.clock)

    def test_expired_bootstrap_reads_recovery_and_releases_the_stopped_claim(self) -> None:
        self.lose_mutation_response(hana_box.Action.BOOTSTRAP)
        calls = list(self.provider.calls)
        _ = self.tick()
        self.assertEqual(len(self.provider.recoveries), 1)
        self.assertEqual(self.provider.calls, calls)
        self.assertIsNone(self.record()['operation'])
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)
        self.assertEqual(self.record()['claims'], [])
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimCommitted)

    def test_expired_start_recovers_a_running_box_and_restores_exposure_before_stopping(self) -> None:
        self.lose_mutation_response(hana_box.Action.START)
        spending = hana_spend.HanaSpending(self.book, self.settings, hana_spend.read_settings(conf.read(self.workload_path)))
        spending.reconcile(self.clock.now, 'stopped', generation=self.generation())
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)
        starts = set(self.provider.starts)
        _ = self.tick()
        self.assertEqual(len(self.provider.recoveries), 1)
        self.assertEqual(self.provider.starts, starts)
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureOpen)
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.STOPPING.value)
        _ = self.tick()
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertEqual(self.record()['claims'], [])
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimCommitted)

    def test_expired_publication_recovers_without_extending_permission(self) -> None:
        self.lose_mutation_response(hana_box.Action.PUBLISH)
        publications = list(self.provider.published)
        self.assertEqual(len(publications), 1)
        _ = self.tick()
        self.assertEqual(self.provider.recoveries, [])
        self.assertEqual(self.provider.published, publications)
        self.assertIsNone(self.record()['delivered'])
        self.assertIsInstance(self.controller.permission('execution-one', 'fixture-boot', self.generation()), hana_box.ClaimRefused)
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.STOPPING.value)
        _ = self.tick()
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertEqual(self.record()['claims'], [])
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimCommitted)

    def test_spending_recovery_still_orders_a_stop_after_the_generation_changes(self) -> None:
        self.ready()
        boot = self.record()['boot']
        claims = ledger.rows(self.record(), 'claims')
        self.assertTrue(claims[0]['active'])
        publications = list(self.provider.published)
        expiry = ledger.instant(ledger.mapping(self.record()['delivered']), 'expires')
        settings = hana_box.read_settings(conf.read(self.workload_path))
        self.clock.now = expiry - (2 * settings.controller_tick_timeout_s + 3 * settings.tick_interval_s)
        self.assertEqual(self.tick(), hana_box.Lifecycle.READY)
        self.assertEqual(ledger.mapping(self.record()['operation'])['action'], hana_box.Action.PUBLISH.value)
        workload = hana_spend.read_settings(conf.read(self.workload_path))
        self.costs({'daily_limit_usd': str(workload.storage_usd_per_month / 60)})
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.record()['reason'], 'daily_limit')
        self.assertEqual(self.record()['boot'], boot)
        self.assertEqual(ledger.rows(self.record(), 'claims'), claims)
        generation = self.generation()
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.crash_after_closing_stop_exposure()
        self.costs({'daily_limit_usd': '20'})
        self.provider.state = 'running'
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.generation(), generation + 1)
        self.assertEqual(ledger.mapping(self.record()['operation'])['action'], hana_box.Action.STOP.value)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.provider.calls[-1].action, hana_box.Action.STOP)
        self.assertEqual(self.provider.published, publications)

    def test_stale_recovery_cannot_settle_a_different_mutation(self) -> None:
        self.lose_mutation_response(hana_box.Action.START)
        operation = self.record()['operation']
        observation = hana_box.BoxObservation(IDENTITY, self.clock.now, 'stopped')
        reply = hana_box.MutationSettled('old-operation', observation)
        with mock.patch.object(self.provider, 'recover', return_value=reply), self.assertRaises(ValueError):
            _ = self.tick()
        self.assertEqual(self.record()['operation'], operation)
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureOpen)

    def test_stale_boot_and_generation_never_authorize_commands(self) -> None:
        self.ready()
        assert isinstance(self.provider.boot, hana_box.VerifiedBoot)
        generation = self.generation()
        self.assertIsInstance(self.controller.permission('execution-one', 'old-boot', generation), hana_box.ClaimRefused)
        self.assertIsInstance(self.controller.permission('execution-one', self.provider.boot.boot_id, generation - 1), hana_box.ClaimRefused)
        self.assertIsInstance(self.controller.permission('execution-two', self.provider.boot.boot_id, generation), hana_box.ClaimRefused)

    def test_stale_boot_response_never_publishes_a_permit(self) -> None:
        _ = self.controller.request('execution-one')
        _ = self.tick()
        _ = self.tick()
        self.provider.boot = hana_box.VerifiedBoot('old-boot', 'old-operation')
        _ = self.tick()
        self.assertFalse(any(row.action == hana_box.Action.PUBLISH for row in self.provider.calls))
        self.assertIsInstance(self.controller.permission('execution-one', 'old-boot', self.generation()), hana_box.ClaimRefused)

    def test_abandonment_stops_a_late_permit_acknowledgment_and_a_ready_box(self) -> None:
        _ = self.controller.request('execution-one')
        for _step in range(3):
            _ = self.tick()
        self.assertEqual(ledger.mapping(self.record()['operation'])['action'], hana_box.Action.PUBLISH.value)

        def reach_deadline() -> None:
            self.clock.now = ledger.instant(ledger.mapping(self.record()['start']), 'deadline')

        self.provider.during_call = reach_deadline
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.provider.during_call = lambda: None
        self.assertEqual(len(self.provider.published), 1)
        self.assertIsNone(self.record()['delivered'])
        self.assertEqual(self.record()['claims'], [])
        _ = self.tick()
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)

        self.ready()
        self.clock.now = ledger.instant(ledger.mapping(self.record()['start']), 'deadline')
        self.assertEqual(self.tick(), hana_box.Lifecycle.READY)
        with mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()):
            self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.record()['claims'], [])
        self.assertIsNone(self.record()['delivered'])
        _ = self.tick()
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)

    def test_stale_acknowledgment_keeps_the_publication_unresolved(self) -> None:
        _ = self.controller.request('execution-one')
        for _step in range(3):
            _ = self.tick()
        self.provider.bad_ack = True
        with self.assertRaises(ValueError):
            _ = self.tick()
        self.assertEqual(ledger.mapping(self.record()['operation'])['action'], hana_box.Action.PUBLISH.value)
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.STARTING.value)

    def test_committed_incoming_claim_prevents_idle_stop(self) -> None:
        self.ready()
        self.assertIsInstance(self.controller.release('execution-one'), hana_placement.HanaClaimReleased)
        self.clock.advance(hana_box.read_settings(conf.read(self.workload_path)).idle_stop_seconds)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimCommitted)
        _ = self.tick()
        self.assertFalse(any(row.action == hana_box.Action.STOP for row in self.provider.calls))
        _ = self.tick()
        self.assertEqual(ledger.rows(self.record(), 'claims')[0]['execution'], 'execution-two')

    def test_idle_stop_intent_refuses_a_concurrent_new_claim(self) -> None:
        self.ready()
        self.assertIsInstance(self.controller.release('execution-one'), hana_placement.HanaClaimReleased)
        self.clock.advance(hana_box.read_settings(conf.read(self.workload_path)).idle_stop_seconds)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        received: list[hana_box.ClaimCommitted | hana_box.ClaimRefused | hana_box.HanaResourcesHeld] = []
        self.provider.during_call = lambda: received.append(self.controller.request('execution-two'))
        _ = self.tick()
        self.assertIsInstance(received[0], hana_box.ClaimRefused)
        self.provider.during_call = lambda: None
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimCommitted)

    def test_interruption_and_unknown_stop_never_authorize_manual_restart(self) -> None:
        self.ready()
        ready = self.controller.path.read_text()
        self.provider.state, self.provider.cause = 'stopped', hana_box.StopCause.INTERRUPTION
        self.assertEqual(self.tick(), hana_box.Lifecycle.INTERRUPTED)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)
        starts = len(self.provider.starts)
        _ = self.tick()
        self.assertEqual(len(self.provider.starts), starts)
        interrupted = self.controller.path.read_text()
        _ = self.controller.path.write_text(ready)
        self.provider.cause = hana_box.StopCause.UNKNOWN
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPED_UNKNOWN)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)
        unknown = self.controller.path.read_text()
        for state, cause, record in (
                (hana_box.Lifecycle.INTERRUPTED, hana_box.StopCause.INTERRUPTION, interrupted),
                (hana_box.Lifecycle.STOPPED_UNKNOWN, hana_box.StopCause.UNKNOWN, unknown)):
            with self.subTest(state=state):
                _ = self.controller.path.write_text(record)
                self.provider.cause = cause
                self.assertEqual(ledger.rows(self.record(), 'claims')[0]['execution'], 'execution-one')
                with mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()):
                    self.assertEqual(self.tick(), state)
                self.assertEqual(self.record()['claims'], [])
                self.assertEqual(self.record()['reason'], 'The Hana start owner exited')
                self.assertEqual(len(self.provider.starts), starts)

    def test_interruption_during_idle_stop_forbids_a_manual_restart(self) -> None:
        self.ready()
        self.assertIsInstance(self.controller.release('execution-one'), hana_placement.HanaClaimReleased)
        self.clock.advance(hana_box.read_settings(conf.read(self.workload_path)).idle_stop_seconds)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.record()['stop_cause'], hana_box.StopCause.IDLE.value)
        self.provider.cause = hana_box.StopCause.INTERRUPTION
        _ = self.tick()
        self.assertEqual(self.tick(), hana_box.Lifecycle.INTERRUPTED)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)
        starts = set(self.provider.starts)
        for _step in range(2):
            self.assertEqual(self.tick(), hana_box.Lifecycle.INTERRUPTED)
        self.assertEqual(self.provider.starts, starts)
        self.assertEqual(self.record()['claims'], [])

    def unclaimed_restart(self, cause: hana_box.StopCause, prior: hana_box.Lifecycle, *, later_stops: int = 0) -> None:
        evidence = self.book.read_snapshot()

        def save(state: ledger.LedgerEvidence) -> None:
            nonlocal evidence
            evidence = deepcopy(state)

        _ = self.enterContext(mock.patch.object(ledger.CostLedger, '_read', side_effect=lambda: deepcopy(evidence)))
        _ = self.enterContext(mock.patch.object(ledger.CostLedger, '_save', side_effect=save))
        self.ready()
        self.assertIsInstance(self.controller.release('execution-one'), hana_placement.HanaClaimReleased)
        self.provider.state, self.provider.cause = 'stopped', cause
        self.assertEqual(self.tick(), prior)
        self.provider.cause = hana_box.StopCause.UNKNOWN
        for _step in range(later_stops):
            self.assertEqual(self.tick(), prior)
        self.clock.advance(82 - later_stops)
        self.provider.state, self.provider.cause = 'running', hana_box.StopCause.UNKNOWN
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureOpen)
        self.clock.advance(6)

    def test_unclaimed_restart_after_interruption_allows_a_later_demand_start(self) -> None:
        self.unclaimed_restart(hana_box.StopCause.INTERRUPTION, hana_box.Lifecycle.INTERRUPTED, later_stops=3)
        self.assertEqual(self.record()['stop_cause'], hana_box.StopCause.UNCLAIMED_AFTER_INTERRUPTION.value)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimCommitted)
        for _step in range(4):
            _ = self.tick()
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.READY.value)
        self.assertEqual(len(self.provider.starts), 2)

    def test_unclaimed_restart_after_unknown_stop_keeps_the_hold(self) -> None:
        self.unclaimed_restart(hana_box.StopCause.UNKNOWN, hana_box.Lifecycle.STOPPED_UNKNOWN)
        self.assertEqual(self.record()['stop_cause'], hana_box.StopCause.UNCLAIMED.value)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPED_UNKNOWN)
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)
        self.assertEqual(len(self.provider.starts), 1)

    def unknown_stop_for_release(self) -> dict[str, object]:
        self.ready()
        running = self.record()
        self.assertIsInstance(self.controller.release('execution-one'), hana_placement.HanaClaimReleased)
        self.provider.state = 'stopped'
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPED_UNKNOWN)
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)
        return running

    def run_unknown_stop_release(self, *flags: str) -> tuple[int, str, str]:
        arguments = ['deploy.py', 'hana-release-unknown-stop', '--directory', str(self.state), *flags]
        with (mock.patch.object(sys, 'argv', arguments), redirect_stdout(StringIO()) as output,
              redirect_stderr(StringIO()) as errors,
              mock.patch.object(deploy, 'Deployment', side_effect=AssertionError('Deployment construction')),
              mock.patch.object(hana_provider.AwsHanaProvider, '__init__', side_effect=AssertionError('Provider construction')),
              mock.patch.object(hana_provider.AwsHanaProvider, 'from_settings', side_effect=AssertionError('Provider settings')),
              mock.patch.object(admin_secrets, 'AdminSecretSession', side_effect=AssertionError('Credential session')),
              mock.patch.object(admin_secrets, 'credentials_file', side_effect=AssertionError('Credential file'))):
            status = deploy.main()
        return status, output.getvalue(), errors.getvalue()

    def test_release_unknown_stop_preview_changes_nothing(self) -> None:
        _ = self.unknown_stop_for_release()
        paths = (self.controller.path, self.book.path, self.state / 'controller.lock')
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        calls = list(self.provider.calls)
        status, output, errors = self.run_unknown_stop_release()
        self.assertEqual((status, errors), (0, ''))
        self.assertEqual(output.splitlines(), [
            'hana-release-unknown-stop: would ask the controller to confirm the stop. Use --approve-release to save the decision.'])
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)
        self.assertEqual(self.provider.calls, calls)

    def test_release_unknown_stop_preview_does_not_create_a_missing_lock(self) -> None:
        lock = self.state / 'controller.lock'
        lock.unlink()
        status, output, errors = self.run_unknown_stop_release()
        self.assertEqual((status, output), (1, ''))
        self.assertEqual(len(errors.splitlines()), 1)
        self.assertFalse(lock.exists())

    def test_release_unknown_stop_refuses_unsafe_saved_state(self) -> None:
        running = self.unknown_stop_for_release()
        stopped = self.record()
        ledger_bytes = self.book.path.read_bytes()
        for change, expected in (
                ('state', 'The saved Hana state must be stopped-unknown.'),
                ('claims', 'Hana still has a claim.'),
                ('operation', 'A Hana operation is pending.'),
                ('demand start', 'Hana has a saved demand start.'),
                ('replacement', 'A saved replacement holds Hana.'),
                ('open', 'Hana compute exposure is still open.'),
                ('missing exposure', 'The Hana generation has no saved closed compute exposure.'),
                ('newer exposure', 'The Hana generation is older than the newest compute exposure.'),
                ('missing ledger', 'The Hana compute ledger is unavailable.')):
            with self.subTest(change=change):
                row = dict(stopped)
                if change == 'state':
                    row['state'] = hana_box.Lifecycle.INTERRUPTED.value
                elif change == 'claims':
                    row['claims'] = running['claims']
                elif change == 'operation':
                    row['operation'] = dict(action='observe', token='fixture-observe', at=self.clock.now,
                        generation=self.generation(), permit=None, expected_generation=0, execution='')
                elif change == 'demand start':
                    row['start'] = running['start']
                elif change == 'replacement':
                    hana_replacement_record.save(self.state, hana_replacement_record.ReplacementAttempt(
                        'fixture-image', 'fixture-attachment', 'fixture-token', self.generation(), 'fixture-digest'))
                elif change == 'open':
                    with self.book.transaction() as state:
                        state.hana_compute[-1] = replace(state.hana_compute[-1], state=ledger.HanaExposureOpen())
                elif change == 'missing exposure':
                    with self.book.transaction() as state:
                        state.hana_compute = []
                        state.hana_lifecycle_fence = ledger.HanaLifecycleUnfenced()
                elif change == 'newer exposure':
                    with self.book.transaction() as state:
                        latest = state.hana_compute[-1]
                        generation = latest.generation + 1
                        state.hana_compute.append(replace(latest, generation=generation))
                        state.hana_lifecycle_fence = ledger.HanaLifecycleFence(self.clock.now, generation)
                else:
                    self.book.path.unlink()
                before = json.dumps(row)
                _ = self.controller.path.write_text(before)
                status, output, errors = self.run_unknown_stop_release('--approve-release')
                self.assertEqual((status, output, errors.splitlines()), (1, '', ['hana-release-unknown-stop: ' + expected]))
                self.assertEqual(self.controller.path.read_text(), before)
                hana_replacement_record.record_path(self.state).unlink(missing_ok=True)
                _ = self.book.path.write_bytes(ledger_bytes)

    def test_release_unknown_stop_keeps_the_old_row_if_publication_fails(self) -> None:
        _ = self.unknown_stop_for_release()
        before = self.controller.path.read_bytes()

        def fail_before_publication(path: Path, content: str) -> None:
            self.assertEqual(path, self.controller.path)
            self.assertEqual(ledger.mapping(cast(object, json.loads(content)))['state'], hana_box.Lifecycle.STOPPING.value)
            with (self.state / 'controller.lock').open('r') as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise OSError('The write failed')

        with mock.patch.object(hana_box, 'write_text_atomic', side_effect=fail_before_publication):
            status, output, errors = self.run_unknown_stop_release('--approve-release')
        self.assertEqual((status, output), (1, ''))
        self.assertEqual(len(errors.splitlines()), 1)
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_release_unknown_stop_allows_normal_claim_admission_and_is_safe_to_repeat(self) -> None:
        running = self.unknown_stop_for_release()
        stopped = self.record()
        for key in ('boot', 'delivered'):
            stopped[key] = running[key]
            self.assertIsNotNone(stopped[key])
        _ = self.controller.path.write_text(json.dumps(stopped))
        ledger_bytes = self.book.path.read_bytes()
        pending = (0, 'hana-release-unknown-stop: release waits for the controller to confirm the stop\n', '')
        self.assertEqual(self.run_unknown_stop_release('--approve-release'), pending)
        released = self.record()
        for key in ('start', 'boot', 'delivered'):
            self.assertIsNone(released[key])
        self.assertEqual(released['state'], hana_box.Lifecycle.STOPPING.value)
        self.assertEqual(released['stop_cause'], hana_box.StopCause.OPERATOR_RELEASE.value)
        self.assertEqual(released['unclaimed_stop_generation'], self.generation())
        self.assertEqual(released['observed_at'], stopped['observed_at'])
        reason = ledger.text(released, 'reason')
        self.assertRegex(reason, r'^hana-release-unknown-stop approved at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$')
        before = self.controller.path.read_bytes(), self.controller.path.stat().st_mtime_ns
        self.assertEqual(self.run_unknown_stop_release(), pending)
        self.assertEqual(self.run_unknown_stop_release('--approve-release'), pending)
        self.assertEqual((self.controller.path.read_bytes(), self.controller.path.stat().st_mtime_ns), before)
        self.assertEqual(self.book.path.read_bytes(), ledger_bytes)
        previous_generation = self.generation()
        self.assertEqual(self.controller.request('execution-two'),
                         hana_box.ClaimRefused('Hana recovery must settle before another claim'))
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertEqual(self.provider.calls[-1].action, hana_box.Action.OBSERVE)
        already_released = (0, 'hana-release-unknown-stop: already released\n', '')
        self.assertEqual(self.run_unknown_stop_release(), already_released)
        self.assertEqual(self.run_unknown_stop_release('--approve-release'), already_released)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimCommitted)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STARTING)
        self.assertEqual(self.provider.calls[-1].action, hana_box.Action.BOOTSTRAP)
        exposure = self.book.read_snapshot().hana_compute[-1]
        self.assertGreater(exposure.generation, previous_generation)
        self.assertIsInstance(exposure.state, ledger.HanaExposureOpen)
        for _step in range(3):
            _ = self.tick()
        assert isinstance(self.provider.boot, hana_box.VerifiedBoot)
        self.assertIsInstance(self.controller.permission('execution-two', self.provider.boot.boot_id, self.generation()),
                              hana_box.CommandPermission)

    def test_release_unknown_stop_preserves_a_confirmed_interruption(self) -> None:
        _ = self.unknown_stop_for_release()
        self.assertEqual(self.run_unknown_stop_release('--approve-release')[0], 0)
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.STOPPING.value)
        self.provider.cause = hana_box.StopCause.INTERRUPTION
        self.assertEqual(self.tick(), hana_box.Lifecycle.INTERRUPTED)
        self.assertEqual(self.provider.calls[-1].action, hana_box.Action.OBSERVE)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)

    def test_release_unknown_stop_stops_a_running_box_before_settlement(self) -> None:
        _ = self.unknown_stop_for_release()
        previous_generation = self.generation()
        self.assertEqual(self.run_unknown_stop_release('--approve-release')[0], 0)
        self.provider.state = 'running'
        call_count = len(self.provider.calls)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertGreater(self.generation(), previous_generation)
        self.assertEqual(self.record()['unclaimed_stop_generation'], self.generation())
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureOpen)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)
        self.assertEqual([call.action for call in self.provider.calls[call_count:]],
                         [hana_box.Action.OBSERVE, hana_box.Action.STOP, hana_box.Action.OBSERVE])

    def save_provider_operation(self, action: hana_box.Action, operation: str, at: int) -> None:
        directory = self.state / 'hana/provider'
        directory.mkdir(exist_ok=True)
        path = directory / (operation + '.json')
        _ = path.write_text(json.dumps(dict(version=1, token=operation, deployment=IDENTITY.deployment,
            action=action.value, dispatched=True, accepted=True)))
        os.utime(path, (at, at))

    def save_setup_generation(self, generation: int) -> None:
        setup = self.state / 'hana/setup.json'
        saved = ledger.mapping(cast(object, json.loads(setup.read_text())))
        _ = setup.write_text(json.dumps(saved | {'generation': generation}))

    def test_accepted_start_then_stale_stopped_observation_cannot_release_the_hold(self) -> None:
        self.assertIsInstance(self.controller.request('execution-one'), hana_box.ClaimCommitted)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STARTING)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STARTING)
        self.assertEqual(self.provider.calls[-1].action, hana_box.Action.START)
        self.assertEqual(len(self.provider.starts), 1)
        generation = self.generation()
        self.save_setup_generation(generation)
        self.save_provider_operation(hana_box.Action.START, self.provider.calls[-1].token, self.clock.now)
        self.provider.state = 'stopped'
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPED_UNKNOWN)
        self.assertIsInstance(self.controller.release('execution-one'), hana_placement.HanaClaimReleased)
        self.provider.state = 'running'
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.generation(), generation + 1)
        self.assertEqual(self.record()['stop_cause'], hana_box.StopCause.UNCLAIMED.value)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPED_UNKNOWN)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)

    def test_saved_accepted_stops_for_both_generations_cannot_release_the_hold(self) -> None:
        self.unclaimed_restart(hana_box.StopCause.UNKNOWN, hana_box.Lifecycle.STOPPED_UNKNOWN)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPED_UNKNOWN)
        prior, latest = self.book.read_snapshot().hana_compute[-2:]
        self.save_setup_generation(prior.generation)
        for exposure in (prior, latest):
            assert isinstance(exposure.state, ledger.HanaExposureClosed)
            self.save_provider_operation(hana_box.Action.STOP, f'fixture-stop-{exposure.generation}', exposure.state.at - 1)
        for _step in range(2):
            self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPED_UNKNOWN)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)

    def assert_saved_stop_survives_a_restart(self) -> None:
        token = ledger.text(ledger.mapping(self.record()['operation']), 'token')
        self.assertFalse(any(call.action == hana_box.Action.STOP for call in self.provider.calls))
        self.controller = hana_box.HanaController(self.state, self.settings, self.workload_path,
                                                  self.provider, clock=self.clock)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.provider.calls[-1].token, token)
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)

    def test_controller_restart_sends_the_saved_stop(self) -> None:
        self.unclaimed_restart(hana_box.StopCause.INTERRUPTION, hana_box.Lifecycle.INTERRUPTED)
        self.assert_saved_stop_survives_a_restart()

    def test_controller_restart_settles_the_saved_stop_after_the_guest_stops(self) -> None:
        self.unclaimed_restart(hana_box.StopCause.INTERRUPTION, hana_box.Lifecycle.INTERRUPTED)
        self.provider.state = 'stopped'
        self.assert_saved_stop_survives_a_restart()

    def test_running_observation_in_the_same_generation_preserves_the_stop_decision(self) -> None:
        self.unclaimed_restart(hana_box.StopCause.INTERRUPTION, hana_box.Lifecycle.INTERRUPTED)
        generation = self.generation()
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.provider.state = 'running'
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.record()['generation'], generation)
        self.assertEqual(self.record()['stop_cause'], hana_box.StopCause.UNCLAIMED_AFTER_INTERRUPTION.value)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)

    def crash_after_closing_stop_exposure(self) -> None:
        with (mock.patch.object(self.controller, '_save', side_effect=RuntimeError('The lifecycle write failed')),
              self.assertRaisesRegex(RuntimeError, 'The lifecycle write failed')):
            _ = self.tick()
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.STOPPING.value)
        self.controller = hana_box.HanaController(self.state, self.settings, self.workload_path,
                                                  self.provider, clock=self.clock)

    def test_pending_generation_cannot_settle_an_older_stop_decision(self) -> None:
        self.unclaimed_restart(hana_box.StopCause.INTERRUPTION, hana_box.Lifecycle.INTERRUPTED)
        generation = self.generation()
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.crash_after_closing_stop_exposure()
        self.assertEqual(self.record()['unclaimed_stop_generation'], generation)
        self.provider.state = 'pending'
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.record()['generation'], generation + 1)
        self.assertEqual(self.record()['unclaimed_stop_generation'], generation)
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureOpen)
        self.provider.state = 'stopped'
        self.assertEqual(self.tick(), hana_box.Lifecycle.INTERRUPTED)
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)

    def test_interruption_during_the_unclaimed_stop_keeps_priority(self) -> None:
        self.unclaimed_restart(hana_box.StopCause.INTERRUPTION, hana_box.Lifecycle.INTERRUPTED)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.provider.cause = hana_box.StopCause.INTERRUPTION
        self.assertEqual(self.tick(), hana_box.Lifecycle.INTERRUPTED)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)

    def test_automatic_restart_is_counted_without_caller_while_disabled(self) -> None:
        text_setting(self.workload_path, 'enabled', 'off')
        self.provider.state = 'running'
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        exposure = self.book.read_snapshot().hana_compute[-1]
        self.assertIsInstance(exposure.state, ledger.HanaExposureOpen)
        self.assertEqual(self.record()['claims'], [])
        self.assertFalse(any(row.action == hana_box.Action.PUBLISH for row in self.provider.calls))
        _ = self.tick()
        _ = self.tick()
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)

    def test_renewal_preserves_execution_and_uses_expected_generation(self) -> None:
        self.ready()
        old = self.generation()
        claim = ledger.rows(self.record(), 'claims')[0]
        expiry = ledger.instant(ledger.mapping(self.record()['delivered']), 'expires')
        settings = hana_box.read_settings(conf.read(self.workload_path))
        self.clock.now = expiry - (2 * settings.controller_tick_timeout_s + 3 * settings.tick_interval_s)
        _ = self.tick()
        intent = ledger.mapping(self.record()['operation'])
        self.assertEqual(intent['action'], hana_box.Action.PUBLISH.value)
        self.assertEqual(intent['expected_generation'], old)
        self.assertGreater(self.generation(), old)
        self.assertEqual(ledger.rows(self.record(), 'claims')[0], claim)
        observed_at = self.record()['observed_at']
        _ = self.tick()
        self.assertEqual(self.record()['observed_at'], observed_at)
        self.assertEqual(ledger.rows(self.record(), 'claims')[0], claim)

    def test_refresh_failure_does_not_stop_lifecycle_recovery(self) -> None:
        _ = self.controller.request('execution-one')
        events: list[str] = []
        def lifecycle() -> object:
            events.append('lifecycle')
            return self.controller.tick()
        def refresh() -> object:
            events.append('refresh')
            raise RuntimeError('The bill service is unavailable')
        with mock.patch.object(sys, 'stderr', StringIO()):
            for _step in range(4):
                self.clock.advance(1)
                self.assertEqual(aws_target.controller_tick(self.settings, self.state, self.workload_path,
                    lifecycle=lifecycle, refresh=refresh), 1)
        self.assertEqual(events, ['lifecycle', 'refresh'] * 4)
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.READY.value)

    def test_controller_tick_logs_the_provider_reason(self) -> None:
        def lifecycle() -> None:
            raise hana_provider.ProviderPending('The Hana bootstrap operation remains unresolved')

        error = StringIO()
        with mock.patch.object(sys, 'stderr', error):
            self.assertEqual(aws_target.controller_tick(self.settings, self.state, self.workload_path,
                                                        lifecycle=lifecycle, refresh=lambda: None), 1)
        self.assertEqual(error.getvalue(),
                         'aws_target: Hana lifecycle unavailable: ProviderPending: '
                         + 'The Hana bootstrap operation remains unresolved\n')

    def test_supervisor_bounds_ticks_and_continues_after_timeout(self) -> None:
        calls: list[float] = []
        def run(arguments: list[str], *, timeout: float, check: bool, text: bool) -> subprocess.CompletedProcess[str]:
            self.assertIn('tick', arguments)
            self.assertTrue(text)
            self.assertFalse(check)
            calls.append(timeout)
            raise subprocess.TimeoutExpired(arguments, timeout)
        pauses: list[float] = []
        settings = hana_box.read_settings(conf.read(self.workload_path))
        with mock.patch.object(sys, 'stderr', StringIO()):
            self.assertEqual(aws_target.serve(self.settings, self.state, self.workload_path, run=run,
                pause=pauses.append, keep_running=lambda: len(calls) < 2), 0)
        self.assertEqual(calls, [settings.controller_tick_timeout_s] * 2)
        self.assertEqual(pauses, [settings.tick_interval_s] * 2)

    def test_serve_cli_dispatches_to_bounded_supervisor(self) -> None:
        with mock.patch.object(sys, 'argv', ['aws_target.py', 'serve', '--settings', str(self.settings),
                '--directory', str(self.state), '--hana-settings', str(self.workload_path)]), \
                mock.patch.object(aws_target, 'serve', return_value=0) as serve:
            self.assertEqual(aws_target.main(), 0)
            serve.assert_called_once_with(self.settings, self.state, self.workload_path)

    def test_caps_changed_before_start_confirm_stopped_without_starting(self) -> None:
        _ = self.controller.request('execution-one')
        _ = self.tick()
        workload = hana_spend.read_settings(conf.read(self.workload_path))
        self.costs({'daily_limit_usd': str(workload.storage_usd_per_month / 60)})
        _ = self.tick()
        self.assertEqual(len(self.provider.starts), 0)
        self.assertEqual(self.record()['reason'], 'daily_limit')
        self.assertIsInstance(self.book.read_snapshot().hana_compute[0].state, ledger.HanaExposureClosed)
        self.assertEqual(len(self.provider.recoveries), 1)
        self.assertEqual(len(ledger.rows(self.record(), 'claims')), 1)

    def test_caps_changed_before_publication_preserve_the_guest_deadline(self) -> None:
        _ = self.controller.request('execution-one')
        for _step in range(3):
            _ = self.tick()
        self.assertEqual(ledger.mapping(self.record()['operation'])['action'], hana_box.Action.PUBLISH.value)
        workload = hana_spend.read_settings(conf.read(self.workload_path))
        self.costs({'daily_limit_usd': str(workload.storage_usd_per_month / 60)})
        _ = self.tick()
        self.assertFalse(any(row.action == hana_box.Action.PUBLISH for row in self.provider.calls))
        self.assertIsNone(self.record()['delivered'])
        self.assertEqual(self.record()['reason'], 'daily_limit')

    def test_automatic_restart_cannot_reuse_an_interrupted_execution(self) -> None:
        self.ready()
        old_boot = self.provider.boot
        self.provider.state, self.provider.cause = 'stopped', hana_box.StopCause.INTERRUPTION
        _ = self.tick()
        assert isinstance(old_boot, hana_box.VerifiedBoot)
        self.provider.state, self.provider.cause = 'running', hana_box.StopCause.UNKNOWN
        self.provider.boot = hana_box.VerifiedBoot('automatic-boot', old_boot.start_token)
        before = sum(row.action == hana_box.Action.PUBLISH for row in self.provider.calls)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(sum(row.action == hana_box.Action.PUBLISH for row in self.provider.calls), before)
        self.assertGreater(len(self.book.read_snapshot().hana_compute), 1)
        self.assertIsInstance(self.controller.permission('execution-one', 'automatic-boot', self.generation()), hana_box.ClaimRefused)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.tick(), hana_box.Lifecycle.INTERRUPTED)

    def test_unprovisioned_tick_makes_no_provider_call(self) -> None:
        directory = self.root / 'empty-state'
        controller = hana_box.HanaController(directory, self.settings, self.workload_path, self.provider, clock=self.clock)
        self.assertEqual(controller.tick(), hana_box.Lifecycle.UNPROVISIONED)
        self.assertEqual(self.provider.calls, [])

    def test_tick_observes_the_box_with_any_setup_margin(self) -> None:
        for margin in ('0.25', '0', '-0.01', 'invalid'):
            with self.subTest(margin=margin):
                text_setting(self.workload_path, 'spot_ceiling_margin', margin)
                self.provider.calls.clear()
                self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
                self.assertEqual([call.action for call in self.provider.calls], [hana_box.Action.OBSERVE])

    def test_obsolete_provider_response_cannot_replace_a_new_operation(self) -> None:
        self.ready()
        def change_operation() -> None:
            document = self.record()
            operation = ledger.mapping(document['operation'])
            operation['token'] = 'new-operation-token'
            _ = (self.state / 'hana/lifecycle.json').write_text(json.dumps(document))
        self.provider.during_call = change_operation
        _ = self.tick()
        self.assertEqual(ledger.mapping(self.record()['operation'])['token'], 'new-operation-token')

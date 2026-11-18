"""Journal one bounded Hana lifecycle step under the shared controller lock."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from importlib import import_module
import json
import os
from pathlib import Path
import time
from typing import Protocol, cast
from uuid import uuid4

from scripts.aws_target import hana_guest, hana_replacement_record, hana_spend, ledger, remote
from scripts.aws_target.controller_lock import control_lock
from scripts.build_queue.placement import HANA_IMAGE_STALE as HANA_IMAGE_STALE
from scripts.lib import conf, process_start
from scripts.lib.atomic_file import write_text_atomic


class Lifecycle(StrEnum):
    UNPROVISIONED = 'unprovisioned'
    IDLE_STOPPED = 'idle-stopped'
    INTERRUPTED = 'interrupted'
    STOPPED_UNKNOWN = 'stopped-unknown'
    STARTING = 'starting'
    READY = 'ready'
    STOPPING = 'stopping'
    REPLACEMENT_PENDING = 'replacement-pending'


class StopCause(StrEnum):
    IDLE = 'idle'
    INTERRUPTION = 'interruption'
    UNKNOWN = 'unknown'


class Action(StrEnum):
    OBSERVE = 'observe'
    BOOTSTRAP = 'bootstrap'
    START = 'start'
    STOP = 'stop'
    PUBLISH = 'publish'


@dataclass(frozen=True)
class LifecycleSettings:
    idle_stop_seconds: int
    start_timeout_seconds: int
    tick_interval_s: int
    controller_tick_timeout_s: int


def read_settings(configuration: conf.ConfigRead) -> LifecycleSettings:
    settings = LifecycleSettings(*(configuration.whole(key) for key in
        ('idle_stop_seconds', 'start_timeout_seconds', 'tick_interval_s', 'controller_tick_timeout_s')))
    safety = hana_guest.read_settings(configuration)
    if (min(settings.idle_stop_seconds, settings.start_timeout_seconds, settings.tick_interval_s,
            settings.controller_tick_timeout_s) <= 0
            or settings.tick_interval_s + settings.controller_tick_timeout_s >= safety.guard_grace_seconds
            or settings.start_timeout_seconds + 3 * settings.controller_tick_timeout_s >= safety.permit_max_seconds):
        raise ValueError('Hana lifecycle intervals exceed the guest safety windows')
    return settings


@dataclass(frozen=True)
class DeploymentIdentity:
    deployment: str
    instance: str
    spot_request: str
    region: str


@dataclass(frozen=True)
class NoDeployment:
    """Setup has not registered an approved Hana deployment."""


@dataclass(frozen=True)
class VerifiedBoot:
    """The provider verified this root receiver response against the demand-start challenge."""

    boot_id: str
    start_token: str


@dataclass(frozen=True)
class BootUnverified:
    """No current root receiver response establishes the guest boot identity."""


@dataclass(frozen=True)
class LocalClaimOwner:
    pid: int
    start: str


@dataclass(frozen=True)
class UnrecordedClaimOwner:
    """The lifecycle journal has no process identity for this claim."""


@dataclass(frozen=True)
class WorkClaim:
    execution: str
    token: str
    requested_at: int
    active: bool = False
    owner: LocalClaimOwner | UnrecordedClaimOwner = UnrecordedClaimOwner()


@dataclass(frozen=True)
class DemandStart:
    token: str
    at: int
    deadline: int


@dataclass(frozen=True)
class NoDemandStart:
    """No demand start owns the current boot."""


@dataclass(frozen=True)
class DeliveredPermit:
    generation: int
    expires: int
    execution: str


@dataclass(frozen=True)
class NoDeliveredPermit:
    """The guest has not acknowledged a permit for the current claim."""


@dataclass(frozen=True)
class PendingOperation:
    action: Action
    token: str
    at: int
    generation: int
    permit: hana_guest.WorkPermit | hana_guest.NoWorkPermit = hana_guest.NoWorkPermit()
    expected_generation: int = 0
    execution: str = ''


@dataclass(frozen=True)
class NoOperation:
    """No provider response remains unresolved."""


@dataclass(frozen=True)
class HanaLifecycleEvidence:
    deployment: DeploymentIdentity
    state: Lifecycle
    boot: VerifiedBoot | BootUnverified
    operation: PendingOperation | NoOperation
    permit: DeliveredPermit | NoDeliveredPermit
    claims: tuple[WorkClaim, ...]
    observed_at: int


@dataclass(frozen=True)
class HanaUnprovisioned:
    """No approved deployment exists."""


@dataclass(frozen=True)
class HanaLifecycleUnavailable:
    reason: str
    observed_at: int


@dataclass
class BoxRecord:
    identity: DeploymentIdentity
    state: Lifecycle
    generation: int = 0
    claims: list[WorkClaim] = field(default_factory=list)
    start: DemandStart | NoDemandStart = NoDemandStart()
    boot: VerifiedBoot | BootUnverified = BootUnverified()
    delivered: DeliveredPermit | NoDeliveredPermit = NoDeliveredPermit()
    operation: PendingOperation | NoOperation = NoOperation()
    idle_since: int = 0
    observed_at: int = 0
    stop_cause: StopCause = StopCause.UNKNOWN
    reason: str = ''


@dataclass(frozen=True)
class BoxObservation:
    identity: DeploymentIdentity
    at: int
    state: ledger.InstanceState
    boot: VerifiedBoot | BootUnverified = BootUnverified()
    stop_cause: StopCause = StopCause.UNKNOWN


@dataclass(frozen=True)
class OperationAccepted:
    token: str


@dataclass(frozen=True)
class PermitAccepted:
    token: str
    identity: DeploymentIdentity
    boot: VerifiedBoot
    generation: int
    execution: str


@dataclass(frozen=True)
class MutationUnresolved:
    """The original mutation can still complete after this observation."""

    token: str
    observed: BoxObservation


@dataclass(frozen=True)
class MutationSettled:
    """The provider confirms that this mutation cannot produce further changes."""

    token: str
    observed: BoxObservation


@dataclass(frozen=True)
class StartMayBeRunning:
    """The provider fenced a start that AWS already accepted."""

    token: str
    observed: BoxObservation


MutationRecovery = MutationUnresolved | MutationSettled | StartMayBeRunning


ProviderReply = BoxObservation | OperationAccepted | PermitAccepted
NO_WORK_PERMIT = hana_guest.NoWorkPermit()


class HanaProvider(Protocol):
    def step(self, identity: DeploymentIdentity, start: DemandStart | NoDemandStart,
             operation: PendingOperation) -> ProviderReply:
        """Run one bounded step; require accepted spending before bootstrap, start, or publication."""
        ...

    def recover(self, identity: DeploymentIdentity, start: DemandStart | NoDemandStart,
                operation: PendingOperation, *, abandon_start: bool = False) -> MutationRecovery:
        """Read the outcome and fence an abandoned start before it can run again."""
        ...


class ProviderNotInstalled:
    """D2 installs the concrete AWS and root receiver transport after approved setup."""

    def step(self, identity: DeploymentIdentity, start: DemandStart | NoDemandStart,
             operation: PendingOperation) -> ProviderReply:
        _ = identity, start, operation
        raise RuntimeError('The Hana provider transport is not installed; preserve the pending operation')


    def recover(self, identity: DeploymentIdentity, start: DemandStart | NoDemandStart,
                operation: PendingOperation, *, abandon_start: bool = False) -> MutationRecovery:
        _ = identity, start, operation, abandon_start
        raise RuntimeError('The Hana provider transport is not installed; preserve the pending operation')


@dataclass(frozen=True)
class ClaimCommitted:
    claim: WorkClaim


@dataclass(frozen=True)
class ClaimRefused:
    reason: str


@dataclass(frozen=True)
class ClaimWaiting:
    claim: WorkClaim


@dataclass(frozen=True)
class CommandPermission:
    execution: str
    boot: VerifiedBoot
    generation: int
    expires: int


def _record(row: BoxRecord) -> dict[str, object]:
    operation = row.operation
    permit = operation.permit if isinstance(operation, PendingOperation) else hana_guest.NoWorkPermit()
    return {
        'version': 1, 'identity': dict(deployment=row.identity.deployment, instance=row.identity.instance,
            spot_request=row.identity.spot_request, region=row.identity.region), 'state': row.state.value,
        'generation': row.generation, 'claims': [dict(execution=c.execution, token=c.token,
            requested_at=c.requested_at, active=c.active,
            owner=dict(pid=c.owner.pid, start=c.owner.start) if isinstance(c.owner, LocalClaimOwner) else None)
            for c in row.claims],
        'start': dict(token=row.start.token, at=row.start.at, deadline=row.start.deadline)
            if isinstance(row.start, DemandStart) else None,
        'boot': dict(boot_id=row.boot.boot_id, start_token=row.boot.start_token) if isinstance(row.boot, VerifiedBoot) else None,
        'delivered': dict(generation=row.delivered.generation, expires=row.delivered.expires, execution=row.delivered.execution)
            if isinstance(row.delivered, DeliveredPermit) else None,
        'operation': dict(action=operation.action.value, token=operation.token, at=operation.at,
            generation=operation.generation, expected_generation=operation.expected_generation, execution=operation.execution,
            permit=dict(expires_at=int(permit.expires_at), generation=permit.generation, boot_id=permit.boot_id)
                if isinstance(permit, hana_guest.WorkPermit) else None) if isinstance(operation, PendingOperation) else None,
        'idle_since': row.idle_since, 'observed_at': row.observed_at, 'stop_cause': row.stop_cause.value, 'reason': row.reason,
    }


def _boot_is_current(row: BoxRecord, boot: VerifiedBoot | BootUnverified) -> bool:
    return (isinstance(boot, VerifiedBoot) and bool(boot.boot_id)
        and isinstance(row.start, DemandStart) and boot.start_token == row.start.token
        and (isinstance(row.boot, BootUnverified) or row.boot == boot))


def _empty_text(document: dict[str, object], key: str) -> str:
    value = document[key]
    if not isinstance(value, str):
        raise ValueError(key + ': expected text')
    return value


def _claim_owner(document: dict[str, object]) -> LocalClaimOwner | UnrecordedClaimOwner:
    if document.get('owner') is None:
        return UnrecordedClaimOwner()
    owner = ledger.mapping(document['owner'])
    pid = ledger.instant(owner, 'pid')
    if not pid:
        raise ValueError('The Hana claim owner has no process ID')
    return LocalClaimOwner(pid, ledger.text(owner, 'start'))


def _start_deadline_refusal(row: BoxRecord, now: int, settings: LifecycleSettings) -> str:
    if not row.claims:
        return 'The Hana start has no owner'
    claim = row.claims[0]
    deadline = (row.start.deadline if row.state == Lifecycle.STARTING and isinstance(row.start, DemandStart)
                else claim.requested_at + settings.start_timeout_seconds)
    if not claim.active and now >= deadline:
        return 'The Hana start deadline expired'
    return ''


def _claim_owner_refusal(row: BoxRecord) -> str:
    if row.claims and isinstance(claim_owner := row.claims[0].owner, LocalClaimOwner):
        owner = process_start.process_start(claim_owner.pid)
        if isinstance(owner, (process_start.Gone, process_start.Zombie)) or (
                isinstance(owner, process_start.Started) and owner.start != claim_owner.start):
            return 'The Hana start owner exited'
    return ''


def _start_refusal(row: BoxRecord, now: int, settings: LifecycleSettings) -> str:
    return _start_deadline_refusal(row, now, settings) or _claim_owner_refusal(row)


def _decode(document: dict[str, object]) -> BoxRecord:
    if document.get('version') != 1:
        raise ValueError('The Hana lifecycle journal version is unknown')
    identity = ledger.mapping(document['identity'])
    names = tuple(ledger.text(identity, key) for key in ('deployment', 'instance', 'spot_request', 'region'))
    if not all(names):
        raise ValueError('The Hana deployment identity is incomplete')
    row = BoxRecord(DeploymentIdentity(*names), Lifecycle(ledger.text(document, 'state')),
        ledger.instant(document, 'generation'),
        [WorkClaim(ledger.text(c, 'execution'), ledger.text(c, 'token'), ledger.instant(c, 'requested_at'),
                   c['active'] is True, _claim_owner(c))
         for c in ledger.rows(document, 'claims')], idle_since=ledger.instant(document, 'idle_since'),
        observed_at=ledger.instant(document, 'observed_at'), stop_cause=StopCause(ledger.text(document, 'stop_cause')),
        reason=_empty_text(document, 'reason'))
    if len(row.claims) > 1 or any(not c.execution or not c.token for c in row.claims):
        raise ValueError('The Hana claim record is invalid')
    if document['start'] is not None:
        item = ledger.mapping(document['start'])
        row.start = DemandStart(ledger.text(item, 'token'), ledger.instant(item, 'at'), ledger.instant(item, 'deadline'))
    if document['boot'] is not None:
        item = ledger.mapping(document['boot'])
        row.boot = VerifiedBoot(ledger.text(item, 'boot_id'), ledger.text(item, 'start_token'))
    if document['delivered'] is not None:
        item = ledger.mapping(document['delivered'])
        row.delivered = DeliveredPermit(ledger.instant(item, 'generation'), ledger.instant(item, 'expires'), ledger.text(item, 'execution'))
    if document['operation'] is not None:
        item = ledger.mapping(document['operation'])
        permit: hana_guest.WorkPermit | hana_guest.NoWorkPermit = hana_guest.NoWorkPermit()
        if item['permit'] is not None:
            raw = ledger.mapping(item['permit'])
            permit = hana_guest.WorkPermit(float(ledger.instant(raw, 'expires_at')), ledger.text(raw, 'generation'), ledger.text(raw, 'boot_id'))
        row.operation = PendingOperation(Action(ledger.text(item, 'action')), ledger.text(item, 'token'), ledger.instant(item, 'at'),
            ledger.instant(item, 'generation'), permit, ledger.instant(item, 'expected_generation'), _empty_text(item, 'execution'))
    return row


def require_idle_setup(path: Path, identity: DeploymentIdentity) -> None:
    """Confirm that the saved lifecycle allows maintenance."""
    try:
        row = _decode(remote.read_record(path))
    except FileNotFoundError:
        raise ValueError('The saved Hana setup has no lifecycle registration. Complete approved setup first.') from None
    except (KeyError, ValueError, TypeError):
        raise ValueError('The Hana lifecycle cannot confirm an idle box. Run hana-setup again when the box is idle.') from None
    if row.identity != identity:
        raise ValueError('The Hana lifecycle registration differs from the saved setup.')
    if (row.state != Lifecycle.IDLE_STOPPED or row.claims or isinstance(row.start, DemandStart)
            or isinstance(row.operation, PendingOperation) and row.operation.action != Action.OBSERVE):
        raise ValueError('The Hana lifecycle is busy. Run hana-setup again when the box is idle.')


def record_replacement_stop(path: Path, generation: int, now: int, *, stopped: bool) -> None:
    """Record the handoff of the stopped or stopping box."""
    previous = _decode(remote.read_record(path))
    state = Lifecycle.IDLE_STOPPED if stopped else Lifecycle.STOPPING
    row = BoxRecord(previous.identity, state, generation=generation, idle_since=now, observed_at=now,
                    stop_cause=StopCause.IDLE)
    write_text_atomic(path, json.dumps(_record(row), sort_keys=True) + '\n')


class HanaController:
    """Serialize claims and durable intents before releasing the lock for provider calls."""

    def __init__(self, directory: Path, settings_path: Path, workload_path: Path, provider: HanaProvider,
                 *, clock: Callable[[], float] = time.time):
        self.directory: Path = directory
        self.path: Path = directory / 'hana' / 'lifecycle.json'
        self.settings_path: Path = settings_path
        self.workload_path: Path = workload_path
        self.provider: HanaProvider = provider
        self.clock: Callable[[], float] = clock

    def _read(self) -> BoxRecord | NoDeployment:
        try:
            return _decode(ledger.mapping(cast(object, json.loads(self.path.read_text()))))
        except FileNotFoundError:
            return NoDeployment()

    def _save(self, row: BoxRecord) -> None:
        write_text_atomic(self.path, json.dumps(_record(row), sort_keys=True) + '\n')

    def inspect(self) -> HanaLifecycleEvidence | HanaUnprovisioned | HanaLifecycleUnavailable:
        """Read an immutable lifecycle projection without provider calls or journal changes."""
        try:
            with control_lock(self.directory):
                row = self._read()
                if isinstance(row, NoDeployment):
                    return HanaUnprovisioned()
                return HanaLifecycleEvidence(row.identity, row.state, row.boot, row.operation,
                    row.delivered, tuple(row.claims), row.observed_at)
        except (OSError, ValueError, KeyError, TypeError) as error:
            return HanaLifecycleUnavailable(type(error).__name__, int(self.clock()))

    def _spending(self) -> hana_spend.HanaSpending:
        return hana_spend.HanaSpending(ledger.CostLedger(self.directory), self.settings_path,
            hana_spend.read_settings(conf.read(self.workload_path)))

    def image_is_current(self, identity: DeploymentIdentity) -> bool:
        """Check the digest setup recorded for this instance."""
        check = cast(Callable[[Path, str, str, Path], bool],
            import_module('scripts.aws_target.hana_image').deployed_image_is_current)
        return check(self.directory, identity.deployment, identity.instance, self.workload_path)

    def register(self, identity: DeploymentIdentity, *, state: Lifecycle = Lifecycle.IDLE_STOPPED) -> None:
        """Register D2's approved identities without provisioning or enabling placement."""
        with control_lock(self.directory):
            previous = self._read()
            if not isinstance(previous, NoDeployment):
                if previous.identity != identity:
                    raise ValueError('An existing deployment needs explicit replacement')
                return
            if not all((identity.deployment, identity.instance, identity.spot_request, identity.region)):
                raise ValueError('The Hana deployment identity is incomplete')
            self._save(BoxRecord(identity, state, idle_since=int(self.clock())))

    def request(self, execution: str) -> ClaimCommitted | ClaimRefused:
        """Commit incoming work under the same lock that selects an idle stop."""
        if not execution:
            return ClaimRefused('The execution identity is missing')
        with control_lock(self.directory):
            if hana_replacement_record.held(self.directory):
                return ClaimRefused(hana_replacement_record.HELD)
            row = self._read()
            if isinstance(row, NoDeployment):
                return ClaimRefused('Hana is unprovisioned')
            if not self._spending().settings.enabled:
                return ClaimRefused('Hana is disabled')
            if not self.image_is_current(row.identity):
                return ClaimRefused(HANA_IMAGE_STALE)
            if row.state not in (Lifecycle.IDLE_STOPPED, Lifecycle.STARTING, Lifecycle.READY):
                return ClaimRefused('Hana recovery must settle before another claim')
            if isinstance(row.operation, PendingOperation) and row.operation.action == Action.STOP:
                return ClaimRefused('The idle stop already owns the machine')
            if row.claims:
                return ClaimCommitted(row.claims[0]) if row.claims[0].execution == execution else ClaimRefused('Hana is busy')
            pid = os.getpid()
            process = process_start.process_start(pid)
            owner = LocalClaimOwner(pid, process.start) if isinstance(process, process_start.Started) else UnrecordedClaimOwner()
            claim = WorkClaim(execution, uuid4().hex, int(self.clock()), owner=owner)
            row.claims = [claim]
            row.reason = ''
            self._save(row)
            return ClaimCommitted(claim)

    def claim_status(self, execution: str) -> ClaimWaiting | ClaimRefused:
        with control_lock(self.directory):
            row = self._read()
            if isinstance(row, NoDeployment) or not row.claims or row.claims[0].execution != execution:
                return ClaimRefused(row.reason if isinstance(row, BoxRecord) and row.reason else 'No current claim owns this execution')
            claim = row.claims[0]
            if not self.image_is_current(row.identity):
                return ClaimRefused(HANA_IMAGE_STALE)
            if row.state in (Lifecycle.STARTING, Lifecycle.READY):
                settings = read_settings(conf.read(self.workload_path))
                reason = _start_refusal(row, int(self.clock()), settings)
                if reason:
                    return ClaimRefused(reason)
            return ClaimWaiting(claim)

    def bind_execution(self, execution: str) -> None:
        """Retain an assigned execution before its dispatch journal exists."""
        from importlib import import_module

        execution_guard_path = cast(Callable[[Path, str], Path],
            import_module('scripts.aws_target.hana_execution').execution_guard_path)

        if not execution:
            raise ValueError('The Hana execution identity is missing')
        with control_lock(self.directory):
            write_text_atomic(execution_guard_path(self.directory, execution), execution + '\n', new_file_mode=0o600)

    def execution_bound(self, execution: str) -> bool:
        """Retain ownership unless the guard and lifecycle prove that execution never bound."""
        from importlib import import_module

        execution_guard_path = cast(Callable[[Path, str], Path],
            import_module('scripts.aws_target.hana_execution').execution_guard_path)
        try:
            with control_lock(self.directory):
                try:
                    _ = execution_guard_path(self.directory, execution).stat()
                    return True
                except FileNotFoundError:
                    pass
                row = self._read()
                if isinstance(row, NoDeployment):
                    return False
                return (any(claim.execution == execution for claim in row.claims)
                        or isinstance(row.operation, PendingOperation) and row.operation.execution == execution)
        except (OSError, ValueError, KeyError, TypeError):
            return True

    def release(self, execution: str) -> bool:
        """Keep unresolved starts owned until lifecycle recovery settles them."""
        with control_lock(self.directory):
            from importlib import import_module

            module = import_module('scripts.aws_target.hana_execution')
            execution_guard_path = cast(Callable[[Path, str], Path], module.execution_guard_path)
            execution_command_settled = cast(Callable[[Path, str], bool], module.execution_command_settled)

            guarded = execution_guard_path(self.directory, execution).exists()
            if guarded and not execution_command_settled(self.directory, execution):
                return False
            row = self._read()
            if isinstance(row, NoDeployment) or not row.claims or row.claims[0].execution != execution:
                return (guarded and isinstance(row, BoxRecord) and not row.claims
                        and row.state not in (Lifecycle.STARTING, Lifecycle.STOPPING)
                        and not isinstance(row.operation, PendingOperation))
            if row.state in (Lifecycle.STARTING, Lifecycle.STOPPING) or isinstance(row.operation, PendingOperation):
                return False
            row.claims = []
            row.delivered = NoDeliveredPermit()
            row.idle_since = int(self.clock())
            self._save(row)
            return True

    def permission(self, execution: str, boot_id: str, generation: int) -> CommandPermission | ClaimRefused:
        with control_lock(self.directory):
            if hana_replacement_record.held(self.directory):
                return ClaimRefused(hana_replacement_record.HELD)
            row = self._read()
            now = int(self.clock())
            if (isinstance(row, NoDeployment) or row.state != Lifecycle.READY or not row.claims
                    or not row.claims[0].active or row.claims[0].execution != execution
                    or not isinstance(row.boot, VerifiedBoot) or row.boot.boot_id != boot_id
                    or not isinstance(row.delivered, DeliveredPermit) or row.delivered.execution != execution
                    or row.delivered.generation != generation or row.generation != generation or now >= row.delivered.expires):
                return ClaimRefused('The claim, boot identity, or delivered permit is stale')
            if not self.image_is_current(row.identity):
                return ClaimRefused(HANA_IMAGE_STALE)
            spending = self._spending().bind_start(now, generation, boot_id)
            if isinstance(spending, hana_spend.PermitRefused):
                return ClaimRefused(spending.reason)
            return CommandPermission(execution, row.boot, generation, row.delivered.expires)

    def _intent(self, row: BoxRecord, action: Action, now: int, *, permit: hana_guest.WorkPermit | hana_guest.NoWorkPermit = NO_WORK_PERMIT,
                expected_generation: int = 0) -> PendingOperation:
        operation = PendingOperation(action, uuid4().hex, now, row.generation, permit, expected_generation,
            row.claims[0].execution if row.claims else '')
        row.operation = operation
        self._save(row)
        return operation

    def tick(self) -> Lifecycle:
        """Run one provider step without holding the controller or ledger lock during network work."""
        now = int(self.clock())
        with control_lock(self.directory):
            row = self._read()
            if isinstance(row, NoDeployment):
                return Lifecycle.UNPROVISIONED
            owner_refusal = _claim_owner_refusal(row)
            if owner_refusal and row.state not in (Lifecycle.STARTING, Lifecycle.READY):
                row.reason, row.claims, row.delivered = owner_refusal, [], NoDeliveredPermit()
                self._save(row)
            settings = read_settings(conf.read(self.workload_path))
            spending = self._spending()
            replacement = hana_replacement_record.read_stop_duty(self.directory)
            if isinstance(replacement, hana_replacement_record.ReplacementStopDuty):
                if any(exposure.generation == replacement.generation and exposure.expires > now
                       and isinstance(exposure.state, ledger.HanaExposureOpen)
                       for exposure in spending.book.read_snapshot().hana_compute):
                    return row.state
                if isinstance(row.operation, PendingOperation) and row.operation.action not in (Action.OBSERVE, Action.STOP):
                    return row.state
            stale_start = bool(row.claims and not row.claims[0].active and not self.image_is_current(row.identity))
            if isinstance(row.operation, NoOperation):
                if (isinstance(replacement, hana_replacement_record.NoReplacement) and row.state == Lifecycle.IDLE_STOPPED
                        and row.claims and spending.settings.enabled):
                    refusal = HANA_IMAGE_STALE if stale_start else _start_deadline_refusal(row, now, settings)
                    if refusal:
                        row.reason, row.claims = refusal, []
                        self._save(row)
                        return row.state
                    reserved = spending.reserve_start(now, now + spending.settings.permit_max_seconds)
                    if isinstance(reserved, hana_spend.PermitRefused):
                        row.reason, row.claims = reserved.reason, []
                        self._save(row)
                        return row.state
                    row.generation = reserved.exposure.generation
                    row.start = DemandStart(uuid4().hex, now, row.claims[0].requested_at + settings.start_timeout_seconds)
                    row.boot, row.delivered = BootUnverified(), NoDeliveredPermit()
                    row.state, row.stop_cause = Lifecycle.STARTING, StopCause.UNKNOWN
                    _ = self._intent(row, Action.BOOTSTRAP, now)
                else:
                    _ = self._intent(row, Action.OBSERVE, now)
            operation = row.operation
            assert isinstance(operation, PendingOperation)
            abandonment = ''
            if row.state == Lifecycle.STARTING or (row.state == Lifecycle.READY and row.claims):
                abandonment = (HANA_IMAGE_STALE if stale_start else _start_deadline_refusal(row, now, settings)
                               or owner_refusal)
            if abandonment and operation.action not in (Action.BOOTSTRAP, Action.START):
                self._stop_abandoned(row, now, abandonment)
                return row.state
            recover = bool(abandonment)
            if abandonment:
                row.reason = abandonment
                self._save(row)
            elif operation.action in (Action.BOOTSTRAP, Action.START, Action.PUBLISH):
                allowed = spending.check_start(now, operation.generation)
                if isinstance(allowed, hana_spend.PermitRefused):
                    row.reason = allowed.reason
                    self._save(row)
                    recover = True
            identity, start = row.identity, row.start
        reply = (self.provider.recover(identity, start, operation, abandon_start=bool(abandonment)) if recover
                 else self.provider.step(identity, start, operation))
        with control_lock(self.directory):
            current = self._read()
            if (isinstance(current, NoDeployment) or current.identity != identity
                    or current.operation != operation or hana_replacement_record.read_stop_duty(self.directory) != replacement):
                return current.state if isinstance(current, BoxRecord) else Lifecycle.UNPROVISIONED
            row = current
            now = int(self.clock())
            if isinstance(reply, (MutationUnresolved, MutationSettled, StartMayBeRunning)):
                self._recover(row, operation, reply, settings, now, abandon_start=bool(abandonment))
            elif isinstance(reply, BoxObservation):
                if operation.action != Action.OBSERVE or reply.identity != row.identity or not operation.at <= reply.at <= now:
                    raise ValueError('The provider observation has a stale identity or time')
                if isinstance(replacement, hana_replacement_record.ReplacementStopDuty):
                    if reply.state == 'stopped':
                        row.state, row.operation = Lifecycle.IDLE_STOPPED, NoOperation()
                    elif reply.state == 'running':
                        self._stop(row, now, StopCause.IDLE)
                    else:
                        row.operation = NoOperation()
                else:
                    self._observe(row, reply, settings, now)
            elif isinstance(reply, OperationAccepted):
                if reply.token != operation.token or operation.action not in (Action.BOOTSTRAP, Action.START, Action.STOP):
                    raise ValueError('The provider accepted a different operation')
                if row.state == Lifecycle.STARTING and operation.action in (Action.BOOTSTRAP, Action.START):
                    reason = _start_refusal(row, now, settings)
                    if reason:
                        row.reason = reason
                        self._save(row)
                        return row.state
                row.operation = NoOperation()
                if operation.action == Action.BOOTSTRAP:
                    _ = self._intent(row, Action.START, now)
                else:
                    _ = self._intent(row, Action.OBSERVE, now)
            else:
                reason = _start_refusal(row, now, settings)
                if reason:
                    self._stop_abandoned(row, now, reason)
                else:
                    self._accept_permit(row, operation, reply, now)
            self._save(row)
            return row.state

    def _recover(self, row: BoxRecord, operation: PendingOperation, reply: MutationRecovery,
                 settings: LifecycleSettings, now: int, *, abandon_start: bool) -> None:
        observed = reply.observed
        if (reply.token != operation.token or observed.identity != row.identity
                or not max(operation.at, row.observed_at) <= observed.at <= now):
            raise ValueError('The mutation recovery has a stale operation, identity, or time')
        active = observed.state in ('pending', 'running', 'stopping', 'shutting-down')
        if active:
            spending = self._spending()
            spending.reconcile(observed.at, observed.state, generation=row.generation)
            row.generation = max((r.generation for r in spending.book.read_snapshot().hana_compute), default=0)
            row.observed_at = observed.at
        if isinstance(reply, MutationUnresolved):
            # A stopped observation cannot close exposure while the original mutation can still complete.
            return
        if active or isinstance(reply, StartMayBeRunning):
            if abandon_start:
                row.claims = []
            valid_boot = _boot_is_current(row, observed.boot)
            self._stop(row, now, StopCause.IDLE if valid_boot else StopCause.UNKNOWN)
        elif abandon_start and observed.state == 'stopped':
            spending = self._spending()
            spending.reconcile(observed.at, 'stopped', generation=row.generation)
            if any(isinstance(r.state, ledger.HanaExposureOpen) for r in spending.book.read_snapshot().hana_compute):
                return
            row.state, row.claims, row.operation = Lifecycle.IDLE_STOPPED, [], NoOperation()
            row.start, row.boot, row.delivered = NoDemandStart(), BootUnverified(), NoDeliveredPermit()
            row.idle_since, row.observed_at = now, observed.at
        else:
            if abandon_start:
                row.claims = []
            self._observe(row, observed, settings, now)

    def _stop_abandoned(self, row: BoxRecord, now: int, reason: str) -> None:
        row.reason, row.claims = reason, []
        self._stop(row, now, StopCause.IDLE if _boot_is_current(row, row.boot) else StopCause.UNKNOWN)

    def _stop(self, row: BoxRecord, now: int, cause: StopCause) -> None:
        row.state, row.stop_cause, row.delivered = Lifecycle.STOPPING, cause, NoDeliveredPermit()
        _ = self._intent(row, Action.STOP, now)

    def _observe(self, row: BoxRecord, observed: BoxObservation, settings: LifecycleSettings, now: int) -> None:
        if observed.at < row.observed_at:
            return
        spending = self._spending()
        spending.reconcile(observed.at, observed.state, generation=row.generation)
        snapshot = spending.book.read_snapshot()
        row.generation = max((r.generation for r in snapshot.hana_compute), default=0)
        row.observed_at, row.operation = observed.at, NoOperation()
        if observed.state in ('stopped', 'terminated'):
            stopped = not any(isinstance(r.state, ledger.HanaExposureOpen) for r in snapshot.hana_compute)
            if not stopped:
                return
            row.delivered, row.boot, row.start = NoDeliveredPermit(), BootUnverified(), NoDemandStart()
            if observed.state == 'terminated':
                row.state = Lifecycle.REPLACEMENT_PENDING
            elif observed.stop_cause == StopCause.INTERRUPTION:
                row.state = Lifecycle.INTERRUPTED
            elif row.state == Lifecycle.STOPPING and row.stop_cause == StopCause.IDLE:
                row.state = Lifecycle.IDLE_STOPPED
            elif row.state != Lifecycle.IDLE_STOPPED:
                row.state = Lifecycle.STOPPED_UNKNOWN
            return
        if row.state == Lifecycle.STARTING:
            reason = _start_refusal(row, now, settings)
            if reason:
                row.reason, row.claims = reason, []
                self._stop(row, now, StopCause.IDLE if _boot_is_current(row, observed.boot) else StopCause.UNKNOWN)
                return
        if observed.state != 'running':
            return
        if row.state == Lifecycle.STOPPING:
            self._stop(row, now, row.stop_cause)
            return
        valid_boot = _boot_is_current(row, observed.boot)
        if not spending.settings.enabled or not row.claims or not valid_boot:
            if row.state == Lifecycle.READY and not row.claims and valid_boot and spending.settings.enabled:
                if now - row.idle_since < settings.idle_stop_seconds:
                    return
                self._stop(row, now, StopCause.IDLE)
            elif (row.state != Lifecycle.STARTING or not isinstance(row.start, DemandStart)
                    or now >= row.start.deadline):
                self._stop(row, now, StopCause.UNKNOWN)
            return
        assert isinstance(observed.boot, VerifiedBoot)
        row.boot = observed.boot
        exposures = [r for r in snapshot.hana_compute if r.generation == row.generation]
        if not exposures:
            self._stop(row, now, StopCause.UNKNOWN)
            return
        exposure = exposures[0]
        renewal_margin = 2 * settings.controller_tick_timeout_s + 3 * settings.tick_interval_s
        expected = row.delivered.generation if isinstance(row.delivered, DeliveredPermit) else 0
        if exposure.expires - now <= renewal_margin and isinstance(row.delivered, DeliveredPermit):
            permission = spending.issue(now, now + spending.settings.permit_max_seconds, row.boot.boot_id,
                expected_generation=row.generation)
        else:
            permission = spending.bind_start(now, row.generation, row.boot.boot_id)
        if isinstance(permission, hana_spend.PermitRefused):
            row.reason = permission.reason
            return
        row.generation = permission.exposure.generation
        if (not isinstance(row.delivered, DeliveredPermit) or row.delivered.generation != row.generation
                or row.delivered.execution != row.claims[0].execution):
            _ = self._intent(row, Action.PUBLISH, now, permit=permission.permit, expected_generation=expected)

    def _accept_permit(self, row: BoxRecord, operation: PendingOperation, reply: PermitAccepted, now: int) -> None:
        if (operation.action != Action.PUBLISH or reply.token != operation.token or reply.identity != row.identity
                or reply.boot != row.boot or reply.generation != operation.generation or reply.execution != operation.execution
                or not row.claims or row.claims[0].execution != reply.execution
                or not isinstance(operation.permit, hana_guest.WorkPermit) or now >= operation.permit.expires_at):
            raise ValueError('The permit acknowledgment has a stale operation, claim, boot identity, or generation')
        row.delivered = DeliveredPermit(reply.generation, int(operation.permit.expires_at), reply.execution)
        claim = row.claims[0]
        row.claims = [replace(claim, active=True)]
        row.state, row.operation = Lifecycle.READY, NoOperation()

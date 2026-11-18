"""Reserve Hana spending before a controller delivers a guest permit."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock
import time
from zoneinfo import ZoneInfo

from scripts.aws_target import hana_guest, ledger
from scripts.lib import conf


@dataclass(frozen=True)
class StartPermitSettings:
    enabled: bool
    region: str
    instance_type: str
    spot_max_usd_per_hour: Decimal
    root_gib: int
    data_gib: int
    daily_budget_usd: Decimal
    gp3_usd_per_gib_month: Decimal
    permit_max_seconds: int

    @property
    def storage_usd_per_month(self) -> Decimal:
        return (self.root_gib + self.data_gib) * self.gp3_usd_per_gib_month


@dataclass(frozen=True)
class PermitSettings(StartPermitSettings):
    guard_grace_seconds: int


def _positive_amount(value: str) -> Decimal:
    amount = conf.decimal(value)
    if amount <= 0:
        raise ValueError("expected a positive amount")
    return amount


def read_permit_settings(configuration: conf.ConfigRead) -> StartPermitSettings:
    """Read the limits for a projected or reserved start."""
    return StartPermitSettings(
        enabled=configuration.value('enabled', conf.on_off),
        region=configuration.text('region'),
        instance_type=configuration.text('instance_type'),
        spot_max_usd_per_hour=configuration.value('spot_max_usd_per_hour', _positive_amount),
        root_gib=configuration.value('root_gib', conf.positive_whole),
        data_gib=configuration.value('data_gib', conf.positive_whole),
        daily_budget_usd=configuration.value('daily_budget_usd', _positive_amount),
        gp3_usd_per_gib_month=configuration.value('gp3_usd_per_gib_month', _positive_amount),
        permit_max_seconds=configuration.value('permit_max_seconds', conf.positive_whole),
    )


def read_settings(configuration: conf.ConfigRead) -> PermitSettings:
    """Check guest settings before execution."""
    start = read_permit_settings(configuration)
    grace = configuration.value('guard_grace_seconds', conf.positive_whole)
    guest: dict[str, object] = {
        'enabled': start.enabled, 'permit_max_seconds': start.permit_max_seconds,
        'guard_grace_seconds': grace,
    }
    for key in ('watchdog_check_seconds', 'step_cpu_limit', 'step_memory_gib', 'mount_timeout_seconds'):
        guest[key] = configuration.whole(key)
    _ = hana_guest.decode_settings(guest)
    return PermitSettings(start.enabled, start.region, start.instance_type, start.spot_max_usd_per_hour,
        start.root_gib, start.data_gib, start.daily_budget_usd, start.gp3_usd_per_gib_month,
        start.permit_max_seconds, grace)


@dataclass(frozen=True)
class PermitIssued:
    permit: hana_guest.WorkPermit
    exposure: ledger.HanaComputeExposure


@dataclass(frozen=True)
class PermitRefused:
    reason: str


@dataclass(frozen=True)
class StartReserved:
    exposure: ledger.HanaComputeExposure


@dataclass(frozen=True)
class StartAffordable:
    """A projected start fits the current limits without reserving spending."""
    expires_at: int


@dataclass(frozen=True)
class StorageReserved:
    exposure: ledger.HanaStorageExposure


@dataclass(frozen=True)
class _PreviewFileVersion:
    stamp: conf.ConfigurationStamp
    size: int


@dataclass(frozen=True)
class _PreviewFileAbsent:
    """The file does not exist when the preview checks its inputs."""


type _PreviewInputs = dict[Path, _PreviewFileVersion | _PreviewFileAbsent]
_REPOSITORY_GATE_CONFIG = Path(__file__).resolve().parents[2] / 'config/cloud_gate.conf'


def _preview_inputs(book: ledger.CostLedger, account: Path, workload: Path, lifecycle: Path) -> _PreviewInputs:
    versions: _PreviewInputs = {}
    for path in (book.path, book.launch_history_path, account, workload, lifecycle,
                 conf.path('cloud_gate'), _REPOSITORY_GATE_CONFIG, *book.bill_paths()):
        try:
            stat = path.stat()
        except FileNotFoundError:
            versions[path] = _PreviewFileAbsent()
        else:
            versions[path] = _PreviewFileVersion(conf.ConfigurationStamp.from_stat(stat), stat.st_size)
    return versions


@dataclass(frozen=True)
class _SharedPlacementPreview:
    inputs: _PreviewInputs
    result: StartAffordable | PermitRefused
    checked_at: float
    deadline: float
    monotonic_deadline: float


@dataclass(frozen=True)
class _NoPlacementPreview:
    """The process has no reusable decision for the Hana target."""


class _PlacementPreview:
    """Share the most recent Hana placement preview within the process."""

    def __init__(self) -> None:
        self.lock: Lock = Lock()
        self.cached: _SharedPlacementPreview | _NoPlacementPreview = _NoPlacementPreview()

    def read(self, book: ledger.CostLedger, account: Path, configuration: conf.ConfigRead, lifecycle: Path
             ) -> StartAffordable | PermitRefused:
        with self.lock:
            workload = configuration.source
            inputs = _preview_inputs(book, account, workload, lifecycle)
            version = inputs[workload]
            current_workload = isinstance(version, _PreviewFileVersion) and version.stamp == configuration.stamp
            now, started = time.time(), time.monotonic()
            saved = self.cached
            if (current_workload and isinstance(saved, _SharedPlacementPreview) and saved.inputs == inputs
                    and saved.checked_at <= now < saved.deadline and started < saved.monotonic_deadline):
                return saved.result
            self.cached = _NoPlacementPreview()
            with conf.diagnostics() as configuration_warnings:
                settings = read_permit_settings(configuration)
                try:
                    max_age = configuration.whole('preview_max_age_s')
                except conf.Unusable:
                    max_age = 0
                result = HanaSpending(book, account, settings).preview_start(int(now), int(now) + settings.permit_max_seconds)
            # Git recovery emits a warning and can change without a live file change.
            if (current_workload and max_age > 0 and not configuration_warnings
                    and inputs == _preview_inputs(book, account, workload, lifecycle)):
                self.cached = _SharedPlacementPreview(inputs, result, now, now + max_age, started + max_age)
            return result


_placement_preview = _PlacementPreview()


def preview_placement_start(book: ledger.CostLedger, account: Path, configuration: conf.ConfigRead, lifecycle: Path
                            ) -> StartAffordable | PermitRefused:
    """Project eligibility for a stopped Hana box."""
    return _placement_preview.read(book, account, configuration, lifecycle)


class HanaSpending[Settings: StartPermitSettings = PermitSettings]:
    """Serialize permits and lifecycle evidence with the account ledger."""

    def __init__(self, book: ledger.CostLedger, account_settings: Path, settings: Settings):
        self.book: ledger.CostLedger = book
        self.account_settings: Path = account_settings
        self.settings: Settings = settings

    def _check(self, state: ledger.LedgerEvidence, now: int, *, placement_preview: bool = False
               ) -> PermitRefused | ledger.SpendingAllowed:
        assess = self.book.placement_spending_preview if placement_preview else self.book.spending_decision
        decision = assess(state, self.account_settings, now)
        if not isinstance(decision, ledger.SpendingAllowed):
            return PermitRefused(decision.reason)
        policy = decision.evidence.policy
        if isinstance(policy, ledger.SpendingPolicyUnavailable):
            return PermitRefused(policy.reason)
        daily, _monthly = ledger.spending_periods(ledger.hana_intervals(
            state, now, policy.box_minimum_seconds, reserve=True), ZoneInfo('UTC'))
        today = datetime.fromtimestamp(now, ZoneInfo('UTC')).date().isoformat()
        if any(usd > self.settings.daily_budget_usd for day, usd in daily.items() if day >= today):
            return PermitRefused('hana_daily_budget_usd')
        return decision

    def reserve_storage(self, now: int) -> StorageReserved | PermitRefused:
        """Reserve retained storage before provisioning the box."""
        if not self.settings.enabled:
            return PermitRefused('Hana is disabled')
        with self.book.transaction() as state:
            for row in state.hana_storage:
                if isinstance(row.state, ledger.HanaExposureOpen):
                    decision = self._check(state, now)
                    return decision if isinstance(decision, PermitRefused) else StorageReserved(row)
            proposed = ledger.HanaStorageExposure(now, self.settings.storage_usd_per_month)
            candidate = replace(state, hana_storage=[*state.hana_storage, proposed])
            decision = self._check(candidate, now)
            if isinstance(decision, PermitRefused):
                return decision
            state.hana_storage = candidate.hana_storage
            return StorageReserved(proposed)

    def reserve_setup(self, now: int, expires: int) -> StartReserved | PermitRefused:
        """Reserve approved setup without enabling placement or command permission."""
        return self._reserve_compute(now, expires, expected_generation=0, approved_setup=True)

    def reserve_start(self, now: int, expires: int) -> StartReserved | PermitRefused:
        """Reserve a demand start before its boot identity exists."""
        return self._reserve_compute(now, expires, expected_generation=0)

    def preview_start(self, now: int, expires: int) -> StartAffordable | PermitRefused:
        """Check the same projected exposure without changing account evidence."""
        if not self.settings.enabled:
            return PermitRefused('Hana is disabled')
        if type(now) is not int or type(expires) is not int or now < 0 or not 0 < expires - now <= self.settings.permit_max_seconds:
            return PermitRefused('The permit deadline is outside its renewal window')
        with self.book.locked_snapshot() as state:
            fence = state.hana_lifecycle_fence
            if isinstance(fence, ledger.HanaLifecycleFence) and now < fence.at:
                return PermitRefused('The permit time precedes the current lifecycle fence')
            if any(isinstance(row.state, ledger.HanaExposureOpen) for row in state.hana_compute):
                return PermitRefused('Confirm the previous stop before another start')
            candidate, _proposed = self._project_compute(state, now, expires)
            decision = self._check(candidate, now, placement_preview=True)
            return decision if isinstance(decision, PermitRefused) else StartAffordable(expires)

    def _project_compute(self, state: ledger.LedgerEvidence, now: int, expires: int
                         ) -> tuple[ledger.LedgerEvidence, ledger.HanaComputeExposure]:
        active = [row for row in state.hana_compute if isinstance(row.state, ledger.HanaExposureOpen)]
        generation = max((row.generation for row in state.hana_compute), default=0) + 1
        proposed = ledger.HanaComputeExposure(active[0].at if active else now, expires, generation,
            max(active[0].usd_per_hour, self.settings.spot_max_usd_per_hour) if active else self.settings.spot_max_usd_per_hour,
            instance_type=active[0].instance_type if active else self.settings.instance_type,
            region=active[0].region if active else self.settings.region)
        compute = [row for row in state.hana_compute if isinstance(row.state, ledger.HanaExposureClosed)]
        storage = list(state.hana_storage)
        if not any(isinstance(row.state, ledger.HanaExposureOpen) for row in storage):
            storage.append(ledger.HanaStorageExposure(now, self.settings.storage_usd_per_month))
        return replace(state, hana_compute=[*compute, proposed], hana_storage=storage), proposed

    def check_start(self, now: int, generation: int) -> StartReserved | PermitRefused:
        """Recheck an existing reservation before a start or permit mutation."""
        if not self.settings.enabled:
            return PermitRefused('Hana is disabled')
        with self.book.transaction() as state:
            for row in state.hana_compute:
                if row.generation == generation and isinstance(row.state, ledger.HanaExposureOpen) and now < row.expires:
                    decision = self._check(state, now)
                    return decision if isinstance(decision, PermitRefused) else StartReserved(row)
            return PermitRefused('The start reservation is expired or its generation is old')

    def bind_start(self, now: int, generation: int, boot_id: str) -> PermitIssued | PermitRefused:
        """Authorize an existing reservation after the controller verifies its boot identity."""
        if not boot_id.strip():
            return PermitRefused('The guest boot identity is missing')
        result = self.check_start(now, generation)
        if isinstance(result, PermitRefused):
            return result
        row = result.exposure
        return PermitIssued(hana_guest.WorkPermit(float(row.expires), str(generation), boot_id), row)

    def issue(self, now: int, expires: int, boot_id: str, *, expected_generation: int = 0) -> PermitIssued | PermitRefused:
        """Reserve a start or renewal before returning its increasing generation."""
        if not boot_id.strip():
            return PermitRefused('The guest boot identity is missing')
        result = self._reserve_compute(now, expires, expected_generation=expected_generation)
        if isinstance(result, PermitRefused):
            return result
        return PermitIssued(hana_guest.WorkPermit(float(expires), str(result.exposure.generation), boot_id), result.exposure)

    def _reserve_compute(self, now: int, expires: int, *, expected_generation: int, approved_setup: bool = False) -> StartReserved | PermitRefused:
        if not self.settings.enabled and not approved_setup:
            return PermitRefused('Hana is disabled')
        if type(now) is not int or type(expires) is not int or now < 0 or not 0 < expires - now <= self.settings.permit_max_seconds:
            return PermitRefused('The permit deadline is outside its renewal window')
        with self.book.transaction() as state:
            fence = state.hana_lifecycle_fence
            if isinstance(fence, ledger.HanaLifecycleFence) and now < fence.at:
                return PermitRefused('The permit time precedes the current lifecycle fence')
            active = [row for row in state.hana_compute if isinstance(row.state, ledger.HanaExposureOpen)]
            if expected_generation:
                if not active or active[0].generation != expected_generation:
                    return PermitRefused('The permit generation is old')
                if now >= active[0].expires or expires <= active[0].expires:
                    return PermitRefused('The permit expired or its deadline did not increase')
            elif active:
                return PermitRefused('Confirm the previous stop before another start')
            candidate, proposed = self._project_compute(state, now, expires)
            decision = self._check(candidate, now)
            if isinstance(decision, PermitRefused):
                return decision
            state.hana_compute, state.hana_storage = candidate.hana_compute, candidate.hana_storage
            state.hana_lifecycle_fence = ledger.HanaLifecycleFence(now, proposed.generation)
            return StartReserved(proposed)

    def reconcile(self: HanaSpending[PermitSettings], now: int, observed: ledger.InstanceState, *, generation: int) -> None:
        """Apply an observation only to its generation and after the saved lifecycle time."""
        if type(now) is not int or now < 0:
            raise ValueError('The observation time must use nonnegative integer seconds')
        if type(generation) is not int or generation < 0:
            raise ValueError('The observed generation must use a nonnegative integer')
        with self.book.transaction() as state:
            current_generation = max((row.generation for row in state.hana_compute), default=0)
            if generation != current_generation:
                return
            fence = state.hana_lifecycle_fence
            if isinstance(fence, ledger.HanaLifecycleFence) and now <= fence.at:
                return
            latest = max((row.at for row in (*state.hana_compute, *state.hana_storage)), default=0)
            latest = max([latest, *(row.state.at for row in (*state.hana_compute, *state.hana_storage)
                                    if isinstance(row.state, ledger.HanaExposureClosed))])
            if now < latest:
                return
            if observed in ('stopped', 'terminated'):
                if isinstance(fence, ledger.HanaLifecycleUnfenced) and state.hana_compute:
                    state.hana_lifecycle_fence = ledger.HanaLifecycleFence(now, current_generation)
                    return
                state.hana_compute = [replace(row, state=ledger.HanaExposureClosed(now))
                    if isinstance(row.state, ledger.HanaExposureOpen) else row for row in state.hana_compute]
            elif observed in ('pending', 'running', 'stopping', 'shutting-down'):
                if not any(isinstance(row.state, ledger.HanaExposureOpen) for row in state.hana_compute):
                    current_generation += 1
                    state.hana_compute.append(ledger.HanaComputeExposure(now, now + self.settings.guard_grace_seconds,
                        current_generation, self.settings.spot_max_usd_per_hour,
                        instance_type=self.settings.instance_type, region=self.settings.region))
                if not any(isinstance(row.state, ledger.HanaExposureOpen) for row in state.hana_storage):
                    state.hana_storage.append(ledger.HanaStorageExposure(now, self.settings.storage_usd_per_month))
            state.hana_lifecycle_fence = ledger.HanaLifecycleFence(now, current_generation)

    def confirm_storage_deleted(self, now: int) -> None:
        """Release storage only after the controller confirms volume deletion."""
        with self.book.transaction() as state:
            if any(isinstance(row.state, ledger.HanaExposureOpen) for row in state.hana_compute):
                raise ValueError('Confirm the stop before storage deletion')
            if any(now < row.at for row in state.hana_storage):
                raise ValueError('Storage deletion precedes its creation')
            state.hana_storage = [replace(row, state=ledger.HanaExposureClosed(now))
                if isinstance(row.state, ledger.HanaExposureOpen) else row for row in state.hana_storage]

"""Run cloud gates through a scaler service and retain each attempt locally."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import ast
from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from threading import Lock
import time
from typing import BinaryIO, Literal, cast, final
from uuid import uuid4

from scripts.aws_target import admin_secrets, gate_box, key_refusal, ledger, run_cost, s3_presign
from scripts.aws_target.aws_cli import aws_error_code
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable, control_lock, fetch_account_charges
from scripts.aws_target.config import (AwsOn, account_settings_path, aws_switch, cloud_gate_off_reason, gate_deployment,
    gate_regions, money, read_settings)
from scripts.aws_target.run_cost import GateLaunchVariant as GateLaunchVariant
from scripts.build_queue import admission_client, admission_protocol as wire, cloud_progress, progress_profile, queue_place, snapshot_codec
from scripts.build_queue.admission_wire import CloudGroup, NoCloudGroup, NO_CLOUD_GROUP
from scripts.build_queue import execution_identity, placement
from scripts.build_queue.placement import Available as Available, Unavailable as Unavailable
from scripts.lib import cancellation, git_index
from scripts.build_targets import target_config
from scripts.buildlog import store
from scripts.lib import conf
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob
from scripts.lib.process_start import process_start, Started, Unreadable
from scripts.lib.atomic_file import write_text_atomic
from scripts.build_queue import admission_records
from scripts.production import affected_modules
from scripts.production.gate_modules import GateRefused, is_test_module
from scripts.production import gate
from scripts.style.git_process import parse_tree_listing
from scripts.test_times import gate_record


@dataclass(frozen=True)
class CloudDeclined:
    reason: str



@dataclass
class CloudAttempt:
    directory: Path
    mode: gate.SuiteMode
    identifier: str = ''
    interruptions: int = 0
    gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED
    claim: placement.ProviderClaimReference | placement.NoProviderClaim = placement.NoProviderClaim()
    booking_submitted: bool = False


@dataclass(frozen=True)
class CloudCompleted:
    run: gate.GateRun


NO_ASSIGNED_LAUNCH = placement.NoLaunchPermission('no assignment')


@dataclass(frozen=True)
class RegionalLiveSpotRead:
    region: str
    seconds: float
    used: bool
    prices: tuple[ledger.RegionalSpotPrice, ...] = ()


SPOT_INTERRUPTION_CODES = frozenset(('Server.SpotInstanceTermination', 'Server.SpotInstanceShutdown'))
# The downloaded box result; outcome.json names this file and holds no second copy.
RESULT_FILE = 'remote-result.json'


class LogUploadFailed(ValueError):
    """The box published a result after its final log upload failed."""


class RetryableCloudFailure(StrEnum):
    SPOT = 'interrupted'
    BOX = 'box-failed'
    NONE = 'no retry'


def retryable_cloud_failure(outcome: dict[str, object]) -> RetryableCloudFailure:
    status = outcome.get('status')
    if status == RetryableCloudFailure.SPOT and outcome.get('interruption_code') in SPOT_INTERRUPTION_CODES:
        return RetryableCloudFailure.SPOT
    if status == RetryableCloudFailure.BOX:
        return RetryableCloudFailure.BOX
    return RetryableCloudFailure.NONE


def settings() -> conf.ConfigRead:
    return conf.read_or_repository('cloud_gate')


@dataclass(frozen=True)
class GateQuotaFit:
    known: bool
    full: bool
    recently_refused: bool


@dataclass(frozen=True)
class GateQuotaCapacity:
    quotas: dict[str, ledger.SpotQuota]
    live: dict[ledger.SpotQuotaPool, int]
    refused: frozenset[ledger.SpotQuotaPool]

    def fit(self, variant: GateLaunchVariant) -> GateQuotaFit:
        pool = ledger.SpotQuotaPool(variant.region, ledger.spot_quota_class(variant.instance_type))
        quota = self.quotas.get(pool.region)
        limit = quota.limit(pool.quota_class) if quota is not None else ledger.NoSpotQuota()
        full = isinstance(limit, Decimal) and self.live.get(pool, 0) + ledger.gate_vcpus(variant.instance_type) > limit
        return GateQuotaFit(isinstance(limit, Decimal), full, pool in self.refused)


def gate_quota_capacity(directory: Path, snapshot: ledger.LedgerEvidence, now: float, *,
                        max_age: int, cooldown: int) -> GateQuotaCapacity:
    try:
        quotas = {row.region: row for row in ledger.read_spot_quotas(directory) if 0 <= now - row.read_at <= max_age}
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        quotas = {}
    live = snapshot.live_gate_vcpus(now)
    refused = frozenset(row.pool for row in snapshot.gate_quota_refusals
                        if row.is_active(live.get(row.pool, 0), now, cooldown))
    return GateQuotaCapacity(quotas, live, refused)


def gate_booking(values: conf.ConfigRead, attempt: str, now: int, expires: int) -> ledger.GateReservation:
    return ledger.GateReservation(attempt, now, expires, money(str(values.number('spot_max_usd_per_hour'))),
        money(str(values.number('attempt_allowance_usd'))), instance_type=values.words('instance_types')[0])


def cooling_zones(rows: Sequence[dict[str, object]], now: float, cooldown: int) -> set[tuple[str, str, str]]:
    """Return the region, type, and zone of each refusal in the cooldown window."""
    return {(str(row.get('region', '')), str(row.get('instance_type', '')), str(row.get('availability_zone', '')))
            for row in rows if row.get('result') == 'refused' and row.get('error_code') in run_cost.ZONE_REFUSALS
            and now - cooldown <= float(str(row['time'])) <= now}


def availability(mode: gate.SuiteMode = gate.SuiteMode.GATE) -> Available | Unavailable:
    """Check local deployment, capacity and spending evidence without reserving resources."""
    try:
        values = settings()
        switch = aws_switch(values)
        if not isinstance(switch, AwsOn):
            return Unavailable(cloud_gate_off_reason(switch))
        directory = target_config.ssh_target_state_directory('aws')
        path = directory / 'gate-deployment.json'
        if not path.is_file():
            return Unavailable('image or stack is absent')
        policy_path = account_settings_path()
        policy = conf.read(policy_path)
        deployment = read(path)
        if 'regions' not in deployment:
            deployment = {**deployment, 'regions': [{**deployment, 'region': deployment.get('region', policy.text('region'))}]}
        regions = [{**row, 'subnets': row.get('subnets', [])} for row in ledger.rows(deployment, 'regions')
                   if row.get('image') and row.get('template')]
        variants = run_cost.deployed_gate_variants({**deployment, 'regions': regions}, values.words('instance_types')).variants
        if not variants:
            return Unavailable('image or stack is absent for configured gate types')
        book = ledger.CostLedger(directory)
        snapshot = book.read_snapshot()
        checked_at = time.time()
        now = int(checked_at)
        cooldown = values.whole('capacity_cooldown_seconds')
        history = run_cost.read_placement_run_history(directory, checked_at, values, policy, policy.text('region'))
        eligible = run_cost.gate_variant_eligibility(variants, history.evidence, mode.value,
                                                    max_gate_minutes=values.whole('max_gate_minutes'))
        if not eligible.variants:
            return Unavailable('Every gate type and region has a median gate time above max_gate_minutes')
        quotas = gate_quota_capacity(directory, snapshot, now, max_age=policy.whole('price_max_age_seconds'),
                                     cooldown=cooldown)
        ready = [variant for variant in eligible.variants if (fit := quotas.fit(variant)).known
                 and not fit.full and not fit.recently_refused]
        if not ready:
            return Unavailable('No gate variant has current Spot quota room')
        zones = cooling_zones(history.launch_rows, checked_at, cooldown)
        if all((variant.region, variant.instance_type, variant.zone) in zones for variant in ready):
            return Unavailable('All gate variants with Spot quota room are in a zone refusal cooldown')
        proposed = gate_booking(values, 'placement-preview', now, now + values.whole('lifetime_minutes') * 60)
        try:
            reason = book.gate_booking_refusal(snapshot, policy_path, proposed)
        except (OSError, ValueError, ArithmeticError) as error:
            return Unavailable(str(error) or 'The gate booking cost is unavailable')
        return Unavailable(reason) if reason else Available()
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, RecursionError):
        return Unavailable('The local AWS gate evidence is unavailable')


def _claim_bookings(episode: str, attempt: int) -> tuple[Path, tuple[ledger.GateReservation, ...]]:
    claim_id = execution_identity.assigned_claim_id(episode, f'{attempt:032x}')
    directory = target_config.ssh_target_state_directory('aws')
    return directory, ledger.CostLedger(directory).gate_claim_reservations(claim_id)


def settlement(episode: str, attempt: int) -> placement.Settled | placement.Pending:
    """Read settlement for the exact claim from the ledger."""
    try:
        _, rows = _claim_bookings(episode, attempt)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        return placement.Pending('The AWS gate ledger is unavailable')
    if any(ledger.gate_is_open(row) for row in rows):
        return placement.Pending('AWS launch settlement remains pending')
    return placement.Settled()


def cleanup_due(reservation: ledger.GateReservation, now: int) -> bool:
    """Select pending cleanup, dead workers, and expired bookings without a known owner."""
    if not ledger.gate_is_open(reservation):
        return False
    if isinstance(reservation.cleanup, ledger.GateCleanupPending):
        return True
    if isinstance(reservation.owner, ledger.GateWorkerOwner):
        current = process_start(reservation.owner.pid)
        return not (isinstance(current, Unreadable)
            or isinstance(current, Started) and current.start == reservation.owner.start)
    return reservation.expires <= now


def read(path: Path) -> dict[str, object]:
    return ledger.mapping(cast(object, json.loads(path.read_text())))


def save(path: Path, row: dict[str, object]) -> None:
    write_text_atomic(path, json.dumps(row, sort_keys=True) + '\n')


def _saved_gate_job(record: dict[str, object]) -> StepJob | GateJobNotRecorded:
    """Read the saved job for an attempt."""
    try:
        return cast(StepJob | GateJobNotRecorded,
            snapshot_codec.parse_snapshot_value(record.get('gate_job'), StepJob | GateJobNotRecorded))
    except (ValueError, OverflowError):
        return GATE_JOB_NOT_RECORDED


def append_launch(directory: Path, row: dict[str, object], *, gate_job: StepJob | GateJobNotRecorded) -> None:
    """Append one launch event with the queue's job record."""
    with store.locked_append(directory / 'cloud-gate-launches.jsonl') as handle:
        store.append_locked(handle, {**row, 'gate_job': snapshot_codec.snapshot_value(gate_job)})


def capacity_error(reason: str) -> bool:
    return any(code in reason for code in ('InsufficientInstanceCapacity', 'InsufficientSpotCapacity',
                                           'MaxSpotInstanceCountExceeded', 'VcpuLimitExceeded', 'SpotMaxPriceTooLow',
                                           '(Unsupported)', '(InvalidInstanceType)', '(InvalidInstanceType.NotFound)'))


def aws_down(reason: str) -> bool:
    return any(code in reason for code in ('Could not connect to the endpoint', 'EndpointConnectionError',
                                           'ServiceUnavailable', 'InternalError', 'RequestTimeout', 'Connection timed out',
                                           'InternalFailure', 'ServerInternal', '(Unavailable)'))


class _PollOutage:
    def __init__(self) -> None:
        self._started_at: float = float('inf')

    def reset(self) -> None:
        self._started_at = float('inf')

    def retry_after_failure(self, error: AwsUnavailable, now: float) -> bool:
        if not aws_down(str(error)):
            return False
        if self._started_at == float('inf'):
            self._started_at = now
        if now - self._started_at >= settings().number('observe_outage_s'):
            return False
        print(f'Cloud gate poll skipped: {error}', flush=True)
        return True


def not_sent(reason: str) -> bool:
    """A connect failure means AWS never received the request."""
    return any(code in reason for code in ('Could not connect to the endpoint', 'EndpointConnectionError'))


def definite_refusal(reason: str) -> bool:
    return 'An error occurred (' in reason and not aws_down(reason)


def launch_error_code(reason: str) -> str:
    code = re.search(r'\(([A-Za-z0-9_.-]+)\)', reason) or re.match(r'[A-Za-z0-9_.-]+', reason)
    return code[1] if code and code.lastindex else code[0] if code else ''


@final
class ModuleProgress:
    """Keep the module total and the names of completed test modules."""
    _header = re.compile(r'Runner: ([0-9]{1,9}) modules, whole machine, [0-9]{1,9} CPUs')
    _result = re.compile(r'(.+) exit=-?[0-9]{1,9} seconds=[0-9]{1,12}\.[0-9]{1,9}(?: stopped)?')

    def __init__(self) -> None:
        self.total = 0
        self.finished: set[str] = set()

    def observe(self, line: str) -> None:
        start = self._header.fullmatch(line)
        if start:
            self.total = int(start[1])
        else:
            result = self._result.fullmatch(line)
            if result and result[1] != '/'.join(gate_record.TYPE_CHECK_JOB):
                self.finished.add(result[1])

    def counter(self) -> progress_profile.ProgressCounter | progress_profile.NoCounter:
        if not self.total:
            return progress_profile.NoCounter('The box has no module counter yet')
        return progress_profile.ProgressCounter('modules', min(len(self.finished), self.total), self.total)


def log_progress(text: str) -> progress_profile.ProgressCounter | progress_profile.NoCounter:
    """Read module progress from the current box log."""
    progress = ModuleProgress()
    for line in text.splitlines():
        progress.observe(line)
    return progress.counter()


def log_cloud_progress(text: str, size_vcpus: int, updated_at: float) -> cloud_progress.CloudProgress:
    """Read the current phase and each module's latest result."""
    state: cloud_progress.CloudState = 'setup' if text.strip() else 'requesting'
    stage = '' if text.strip() else 'launch'
    modules_started = False
    failed: set[str] = set()
    for line in text.splitlines():
        named_stage = re.fullmatch(r'(?:\S+ )?stage=(.+)', line)
        if named_stage and not modules_started:
            stage = named_stage[1]
            if stage.startswith('round '):
                state = 'running'
        start = re.fullmatch(r'Runner: \d+ modules, whole machine, (\d+) CPUs', line)
        if start:
            state, size_vcpus = 'running', int(start[1])
            stage, modules_started = '', True
        result = re.fullmatch(r'(\S+) exit=(-?\d+) seconds=\d+\.\d+(?: stopped)?', line)
        if result:
            if int(result[2]):
                failed.add(result[1])
            else:
                failed.discard(result[1])
        if line == 'Cloud gate: uploading':
            state, stage = 'uploading', 'uploading'
    return cloud_progress.CloudProgress(state, size_vcpus, len(failed), updated_at, stage=stage)


@dataclass(frozen=True)
class GateTermination:
    attempt: str
    targets: ledger.GateCleanupTargets
    owner: ledger.GateWorkerOwner | ledger.GateOwnerUnknown


@dataclass(frozen=True)
class GateTerminationProgress:
    pending: list[GateTermination]
    skipped: bool


@dataclass(frozen=True)
class GateCleanupAttempt:
    reservation: ledger.GateReservation
    launch: dict[str, object]
    launch_record_checked: bool


@dataclass(frozen=True)
class UncertainGateLaunch:
    reservation: ledger.GateReservation
    regions: tuple[str, ...]
    filter: str


@final
class AwsServiceFailure(AwsUnavailable):
    """The AWS CLI reports a service error code for the operation."""
    def __init__(self, operation: str, code: str, *, first_attempt: bool) -> None:
        super().__init__(f'{operation}: {code}')
        self.code = code
        self.first_attempt = first_attempt


def instance_not_found(error: AwsUnavailable) -> bool:
    code = error.code if isinstance(error, AwsServiceFailure) else aws_error_code(error) or str(error).partition(':')[0]
    return code == 'InvalidInstanceID.NotFound'


def cleanup_folder(folder: Path, worker: GateWorker) -> bool:
    """Clean one gate folder or all module folders in a trial."""
    paths = [folder / 'request.json'] if (folder / 'request.json').exists() else sorted(folder.glob('*/request.json'))
    return cleanup_requests(paths, worker)


def cleanup_requests(paths: Sequence[Path], worker: GateWorker) -> bool:
    """Clean only the attempts in the selected request files."""
    attempts: list[GateCleanupAttempt] = []
    complete = True
    for path in paths:
        try:
            attempt = ledger.text(read(path), 'attempt')
            reservation = next((row for row in worker.book.read_snapshot().gate_reservations if row.attempt == attempt), None)
            if reservation is not None:
                launch_path = path.parent / 'launch.json'
                launch = (read(launch_path)
                          if isinstance(reservation.cleanup, ledger.GateCleanupNotStarted) and launch_path.exists() else {})
                attempts.append(GateCleanupAttempt(reservation, launch, True))
        except (OSError, ValueError, KeyError, TypeError) as error:
            print('gate cleanup: ' + str(error), file=sys.stderr)
            complete = False
    return cleanup_attempts(attempts, worker) and complete


def cleanup_attempts(attempts: Sequence[GateCleanupAttempt], worker: GateWorker) -> bool:
    """Request terminations before discovery and retain unconfirmed bookings."""
    complete = True
    deadline = worker.clock() + settings().number('termination_timeout_s')
    boxes: list[GateTermination] = []
    uncertain: list[UncertainGateLaunch] = []
    for item in attempts:
        reservation, launch = item.reservation, item.launch
        if isinstance(reservation.state, (ledger.GateAbsent, ledger.GateTerminated)):
            continue
        if isinstance(reservation.cleanup, ledger.GateCleanupPending):
            boxes.append(GateTermination(reservation.attempt, reservation.cleanup.targets, reservation.owner))
            continue
        try:
            region = str(launch.get('region', reservation.region or worker.api.region))
            instance = str(launch.get('instance', ''))
            if not instance and isinstance(reservation.state, ledger.GateRunning):
                instance = reservation.state.instance_id
            if instance:
                boxes.append(GateTermination(reservation.attempt, ledger.GateCleanupTargets(region, (instance,)),
                                             reservation.owner))
            elif 'arguments' in launch:
                token = ledger.text(ledger.mapping(launch['arguments']), 'ClientToken')
                uncertain.append(UncertainGateLaunch(reservation, (region,), 'Name=client-token,Values=' + token))
            elif item.launch_record_checked:
                worker.book.resolve_gate(reservation.attempt, ledger.GateAbsent(int(worker.clock())),
                                         expected_owner=reservation.owner)
            else:
                regions = (reservation.region,) if reservation.region else tuple(settings().words('regions'))
                uncertain.append(UncertainGateLaunch(reservation, regions, 'Name=tag:GateAttempt,Values=' + reservation.attempt))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            print('gate cleanup: ' + str(error), file=sys.stderr)
            complete = False
    progress = worker.request_terminations(boxes, deadline)
    boxes = progress.pending
    complete = complete and not progress.skipped
    for launch in uncertain:
        reservation = launch.reservation
        try:
            found = False
            all_lookups_after_expiry = True
            for region in launch.regions:
                api = worker.regional_api(region)
                timeout = settings().number('api_timeout_s')
                lookup_started = worker.clock()
                remaining = deadline - lookup_started
                if remaining <= 0:
                    raise AwsUnavailable('The gate cleanup deadline passed')
                reply = api.call('describe-instances', '--filters', launch.filter,
                                 timeout_s=min(timeout, remaining))
                all_lookups_after_expiry = all_lookups_after_expiry and lookup_started >= reservation.expires
                instances = [row for group in ledger.rows(reply, 'Reservations') for row in ledger.rows(group, 'Instances')]
                if instances:
                    targets = tuple(sorted({ledger.text(row, 'InstanceId') for row in instances}))
                    discovered = GateTermination(reservation.attempt, ledger.GateCleanupTargets(region, targets),
                                                 reservation.owner)
                    progress = worker.request_terminations((discovered,), deadline)
                    boxes.extend(progress.pending)
                    complete = complete and not progress.skipped
                    found = True
                    break
            if not found:
                if not launch.regions or not all_lookups_after_expiry:
                    complete = False
                else:
                    worker.book.resolve_gate(reservation.attempt, ledger.GateAbsent(int(worker.clock())),
                                             expected_owner=reservation.owner)
        except (OSError, ValueError, KeyError, TypeError, AwsUnavailable, subprocess.SubprocessError) as error:
            print('gate cleanup: ' + str(error), file=sys.stderr)
            complete = False
    return worker.confirm_terminations(boxes, deadline) and complete


@dataclass
class CloudReservation:
    sidecar: Path
    status: int = 1


@contextmanager
def cloud_reservation(repo: Path, sidecar: Path, work: gate.SuiteMode | StepJob, *,
                      gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED,
                      group: CloudGroup | NoCloudGroup = NO_CLOUD_GROUP) -> Generator[CloudReservation]:
    """Keep the route's cloud slot until its result or fallback returns."""
    pid = os.getpid()
    start = admission_records.process_start_time(pid)
    if start is None:
        raise GateRefused('The cloud route has no live process identity')
    owner, episode, since = wire.Owner(pid, start), uuid4().hex, time.time()
    step = work.step if isinstance(work, StepJob) else work.value
    job = work if isinstance(work, StepJob) else gate_job
    profile = admission_records.ProgramProfile(step, str(Path(__file__).resolve().parents[2]), str(repo.resolve()),
        admission_records.HistoryPeakSizing(0, admission_records.FirstProgramPeak(0), 0.0))
    def refresh() -> wire.AdmitRequest:
        request = admission_client.step_request([step], str(repo), owner, since, str(sidecar), episode,
            profile, run=wire.RunIdentity(pid, start))
        return replace(request, place=wire.CloudPlace('aws', group), identity=replace(request.identity, job=job))
    with ExitStack() as held_slot:
        try:
            held = held_slot.enter_context(queue_place.held_reservation(queue_place.RefreshedRequest(refresh)))
        except queue_place.QueuePlaceRefused as error:
            raise GateRefused(str(error)) from error
        reservation = CloudReservation(sidecar)
        try:
            yield reservation
        finally:
            held.status = reservation.status


@final
class GateWorker:
    """Own one durable launch, result transfer, and confirmed termination."""
    def __init__(self, api: AwsCli, book: ledger.CostLedger, settings_path: Path, sidecar: Path, *,
                 clock: Callable[[], float] = time.time, pause: Callable[[float], None] = time.sleep) -> None:
        self.api = api
        self.box_region = api.region
        self.box_zone: str = ''
        self.apis: dict[str, AwsCli] = {api.region: api}
        self.book = book
        self.settings_path = settings_path
        self.sidecar = sidecar
        self.clock = clock
        self.pause = pause
        self.size_vcpus: int = 0
        self.price_read_evidence: tuple[dict[str, object], ...] = ()

    def regional_api(self, region: str) -> AwsCli:
        if region not in self.apis:
            self.apis[region] = self.api.in_region(region)
        return self.apis[region]

    def publish_log(self, folder: Path) -> None:
        text = (folder / 'gate.log').read_text()
        counter = log_progress(text)
        if isinstance(counter, progress_profile.ProgressCounter):
            progress_profile.publish_counter(self.sidecar, counter)
        cloud_progress.publish(self.sidecar, replace(log_cloud_progress(text, self.size_vcpus, self.clock()),
            region=self.box_region, zone=self.box_zone))

    def charges(self, record_query: Callable[[Decimal], None] = lambda _fee: None) -> None:
        now = int(self.clock())
        def fetch(start: int, end: int) -> tuple[ledger.ChargeRead, ...]:
            policy = read_settings(self.settings_path)
            readings = fetch_account_charges(self.api, start, end, now, policy.cost_explorer_read_usd)
            record_query(sum((reading.query_usd for reading in readings), Decimal(0)))
            return readings
        _ = self.book.refresh_charges(self.settings_path, now, fetch)

    def upload(self, bucket: str, key: str, path: Path) -> None:
        _ = self.api.call('put-object', '--bucket', bucket, '--key', key, '--body', str(path), service='s3api')

    def download(self, bucket: str, key: str, path: Path) -> bool:
        try:
            _ = self.api.call('get-object', '--bucket', bucket, '--key', key, str(path), service='s3api')
            return True
        except AwsServiceFailure as error:
            if error.code in ('404', 'NoSuchKey'):
                return False
            raise
        except AwsUnavailable as error:
            if any(code in str(error) for code in ('NoSuchKey', '(404)', 'Not Found')):
                return False
            raise

    def terminate(self, attempt: str, instance: str, owner: ledger.GateWorkerOwner, *,
                  result_seen_at: int | ledger.GateResultNotReceived = ledger.GATE_RESULT_NOT_RECEIVED) -> None:
        targets = ledger.GateCleanupTargets(self.box_region, (instance,))
        with self.book.gate_cleanup_transaction() as transaction:
            cleanup = transaction.mark(attempt, owner, targets, result_seen_at=result_seen_at)
        if isinstance(cleanup, ledger.GateTerminated):
            return
        api = self.regional_api(self.box_region)
        deadline = self.clock() + settings().number('termination_timeout_s')
        requested = False
        while self.clock() < deadline:
            try:
                if not requested:
                    _ = api.call('terminate-instances', '--instance-ids', instance)
                    requested = True
                if api.observe(instance, int(self.clock())).state == 'terminated':
                    break
            except AwsUnavailable as error:
                if instance_not_found(error):
                    break
                # A regional outage repeats the call until the deadline; any other error ends the try.
                if not aws_down(str(error)):
                    raise
                print(f'Cloud gate termination waits on {self.box_region}: {error}', flush=True)
            self.pause(settings().number('result_poll_s'))
        else:
            raise AwsUnavailable('Termination is unconfirmed; the expiry function still owns cleanup')
        self.book.resolve_gate(attempt, ledger.GateTerminated(int(self.clock()), instance), cleanup=targets)

    def termination_call(self, region: str, operation: str, instances: Sequence[str], deadline: float) -> dict[str, object]:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise AwsUnavailable('The trial cleanup deadline passed')
        return self.regional_api(region).call(operation, '--instance-ids', *instances,
            timeout_s=min(settings().number('api_timeout_s'), remaining))

    def confirmed_ends(self, region: str, operation: Literal['terminate-instances', 'describe-instances'],
                       instances: Sequence[str], deadline: float) -> set[str]:
        try:
            reply = self.termination_call(region, operation, instances, deadline)
            if operation == 'terminate-instances':
                return set()
            return {ledger.text(row, 'InstanceId') for group in ledger.rows(reply, 'Reservations')
                    for row in ledger.rows(group, 'Instances')
                    if ledger.text(ledger.mapping(row['State']), 'Name') == 'terminated'}
        except AwsUnavailable as error:
            if instance_not_found(error):
                if len(instances) == 1:
                    return set(instances)
                # A batch refusal does not identify the absent instances.
                return {ended for instance in instances for ended in self.confirmed_ends(region, operation, (instance,), deadline)}
            print('paired cleanup: ' + str(error), file=sys.stderr)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            print('paired cleanup: ' + str(error), file=sys.stderr)
        return set()

    def record_ends(self, boxes: Sequence[GateTermination], region: str, ended: set[str]) -> GateTerminationProgress:
        pending: list[GateTermination] = []
        skipped = False
        for box in boxes:
            if box.targets.region == region and ended.issuperset(box.targets.instances):
                try:
                    self.book.resolve_gate(box.attempt,
                        ledger.GateTerminated(int(self.clock()), box.targets.instances[0]), cleanup=box.targets)
                    continue
                except ledger.GateCleanupSuperseded as error:
                    print('gate cleanup: ' + str(error), file=sys.stderr)
                    skipped = True
                    continue
                except (OSError, ValueError, KeyError, TypeError) as error:
                    print('paired cleanup: ' + str(error), file=sys.stderr)
            pending.append(box)
        return GateTerminationProgress(pending, skipped)

    def request_terminations(self, boxes: Sequence[GateTermination], deadline: float) -> GateTerminationProgress:
        """Save the cleanup state before requesting termination."""
        pending: list[GateTermination] = []
        skipped = False
        for region in dict.fromkeys(box.targets.region for box in boxes):
            current: list[GateTermination] = []
            superseded: list[str] = []
            try:
                with self.book.gate_cleanup_transaction() as transaction:
                    for box in boxes:
                        if box.targets.region != region:
                            continue
                        try:
                            cleanup = transaction.mark(box.attempt, box.owner, box.targets)
                        except ledger.GateCleanupSuperseded as error:
                            superseded.append(str(error))
                        else:
                            if isinstance(cleanup, ledger.GateCleanupPending):
                                current.append(box)
            except (OSError, ValueError, KeyError, TypeError) as error:
                print('gate cleanup: ' + str(error), file=sys.stderr)
                skipped = True
                continue
            for reason in superseded:
                print('gate cleanup: ' + reason, file=sys.stderr)
                skipped = True
            if not current:
                continue
            instances = tuple(dict.fromkeys(instance for box in current for instance in box.targets.instances))
            ended = self.confirmed_ends(region, 'terminate-instances', instances, deadline)
            progress = self.record_ends(current, region, ended)
            pending.extend(progress.pending)
            skipped = skipped or progress.skipped
        return GateTerminationProgress(pending, skipped)

    def confirm_terminations(self, boxes: Sequence[GateTermination], deadline: float) -> bool:
        """Record confirmed terminations under one shared deadline."""
        confirmed: dict[str, set[str]] = {box.targets.region: set() for box in boxes}
        pending = list(boxes)
        skipped = False
        while pending and self.clock() < deadline:
            for region, known in confirmed.items():
                instances = tuple(dict.fromkeys(instance for box in pending if box.targets.region == region
                                                for instance in box.targets.instances if instance not in known))
                if instances and self.clock() < deadline:
                    known.update(self.confirmed_ends(region, 'describe-instances', instances, deadline))
                progress = self.record_ends(pending, region, known)
                pending = progress.pending
                skipped = skipped or progress.skipped
            if pending:
                self.pause(max(0, min(settings().number('result_poll_s'), deadline - self.clock())))
        return not pending and not skipped

    def cleanup_due(self, reservation: ledger.GateReservation) -> bool:
        return cleanup_due(reservation, int(self.clock()))

    def reconcile(self) -> None:
        """Clean expired or dead attempts while preserving live workers."""
        attempts: list[GateCleanupAttempt] = []
        for reservation in self.book.read_snapshot().gate_reservations:
            if not self.cleanup_due(reservation):
                continue
            launch: dict[str, object] = {}
            checked = False
            if (isinstance(reservation.cleanup, ledger.GateCleanupNotStarted)
                    and isinstance(reservation.owner, ledger.GateWorkerOwner)):
                folder = Path(reservation.owner.folder)
                launch_path = folder / 'launch.json'
                try:
                    with suppress(FileNotFoundError):
                        launch = read(launch_path)
                    checked = (folder / 'request.json').exists()
                except (OSError, ValueError, RecursionError) as error:
                    detail = ' '.join(str(error).split())
                    print(f'Gate cleanup could not read {launch_path}: {detail}', file=sys.stderr)
                    continue
            attempts.append(GateCleanupAttempt(reservation, launch, checked))
        _ = cleanup_attempts(attempts, self)

    def record_launch(self, request: dict[str, object], size: str, zone: str,
                      result: Literal['launched', 'refused', 'finished'], reason: str = '', *, gate_s: float = 0) -> None:
        error_code = launch_error_code(reason)
        now = self.clock()
        row: dict[str, object] = {'time': now, 'attempt': request['attempt'], 'instance_type': size,
            'vcpus': self.size_vcpus, 'result': result, 'mode': gate.SuiteMode.step(str(request['mode'])), 'region': self.box_region}
        if result != 'finished' and self.price_read_evidence:
            row['spot_price_reads'] = self.price_read_evidence
        if result == 'finished':
            row['gate_s'] = gate_s
        else:
            row.update({'availability_zone': zone, 'error_code': error_code, 'reason': reason})
        append_launch(self.book.directory, row, gate_job=_saved_gate_job(request))
        if result == 'refused':
            if error_code in run_cost.QUOTA_REFUSALS:
                pool = ledger.SpotQuotaPool(self.box_region, ledger.spot_quota_class(size))
                try:
                    self.book.record_gate_quota_refusal(pool, now)
                except (OSError, ledger.LedgerUnavailable) as error:
                    print(f'Cloud gate could not record quota use in {self.box_region}: {error}', file=sys.stderr)
            print(f'Cloud gate refused: {size} in {zone}: {error_code}', flush=True)

    def live_spot_prices(self, variants: Sequence[GateLaunchVariant]) -> tuple[RegionalLiveSpotRead, ...]:
        """Read deployed regions concurrently under one launch deadline."""
        if not variants:
            return ()
        values = settings()
        sizes = values.words('instance_types')
        regions = tuple(dict.fromkeys(row.region for row in variants))
        deployed = {(row.region, row.instance_type, row.zone) for row in variants}
        started = time.monotonic()
        deadline = started + values.number('spot_price_live_read_timeout_s')
        read_at = int(self.clock())
        call_started: dict[str, float] = {}

        def fetch(region: str) -> RegionalLiveSpotRead:
            begin = time.monotonic()
            call_started[region] = begin
            try:
                remaining = deadline - begin
                if remaining <= 0:
                    return RegionalLiveSpotRead(region, 0, False)
                reply = self.api.in_region(region).call('describe-spot-price-history', '--instance-types', *sizes,
                    '--product-descriptions', 'Linux/UNIX', '--start-time', datetime.fromtimestamp(read_at, UTC).isoformat(),
                    '--no-paginate', '--max-results', '1000', timeout_s=min(self.api.timeout_s, remaining))
                prices = tuple(row for row in ledger.regional_spot_prices(region, reply, sizes, read_at)
                               if (row.region, row.instance_type, row.zone) in deployed)
                ended = time.monotonic()
                used = bool(prices) and ended <= deadline
                return RegionalLiveSpotRead(region, ended - begin, used, prices if used else ())
            except (AwsUnavailable, OSError, ValueError, KeyError, TypeError, ArithmeticError,
                    subprocess.SubprocessError):
                return RegionalLiveSpotRead(region, time.monotonic() - begin, False)

        pool = ThreadPoolExecutor(max_workers=len(regions))
        try:
            pending = {region: pool.submit(fetch, region) for region in regions}
            done, _ = wait(pending.values(), timeout=max(0, deadline - time.monotonic()))
            ended = time.monotonic()
            return tuple(future.result() if future in done else
                         RegionalLiveSpotRead(region, ended - call_started.get(region, started), False)
                         for region, future in pending.items())
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def rank_variants(self, variants: Sequence[GateLaunchVariant], mode: str,
                      live_prices: Sequence[ledger.RegionalSpotPrice] = ()) -> list[GateLaunchVariant]:
        """Prefer available capacity, then the lowest expected USD per run, then the configured type order.

        A type and region whose median box time is above max_gate_minutes is not tried."""
        values = settings()
        types = values.words('instance_types')
        def type_rank(variant: GateLaunchVariant) -> int:
            return types.index(variant.instance_type) if variant.instance_type in types else len(types)
        policy = conf.read(self.settings_path)
        now = self.clock()
        max_age = policy.whole('price_max_age_seconds')
        cooldown = values.whole('capacity_cooldown_seconds')
        history = run_cost.read_placement_run_history(self.book.directory, now, values, policy, self.api.region)
        zones = cooling_zones(history.launch_rows, now, cooldown)
        cap = values.whole('max_gate_minutes')
        eligibility = run_cost.gate_variant_eligibility(variants, history.evidence, mode, max_gate_minutes=cap)
        for (size, region), box in sorted(eligibility.slow_box_seconds.items()):
            print(f'Cloud gate skips {size} in {region}: its median gate time {run_cost.median_run_time([box])} '
                  + f'is above max_gate_minutes={cap}.', flush=True)
        try:
            snapshot = self.book.read_snapshot()
        except ledger.LedgerUnavailable:
            snapshot = ledger.LedgerEvidence()
        quotas = gate_quota_capacity(self.book.directory, snapshot, now, max_age=max_age, cooldown=cooldown)
        def capacity_order(variant: GateLaunchVariant) -> tuple[bool, bool, bool]:
            key = (variant.region, variant.instance_type, variant.zone)
            fit = quotas.fit(variant)
            return fit.recently_refused, fit.full, key in zones
        zone_prices = run_cost.current_zone_prices(self.book.directory, now, max_age)
        zone_prices.update({(row.region, row.instance_type, row.zone): row.usd_per_hour for row in live_prices})
        prices = run_cost.deployed_gate_prices(eligibility.variants, zone_prices)
        if not prices:
            return sorted(eligibility.variants, key=capacity_order)
        pricing = run_cost.GateRunPricing.from_config(values, history.evidence, self.api.region, (mode,))
        def order(item: tuple[int, GateLaunchVariant]) -> tuple[tuple[bool, bool, bool], bool, Decimal, int, int]:
            position, variant = item
            price = prices.get(variant)
            cost = pricing.expected_usd(price, variant.instance_type, variant.region) if price is not None else Decimal(0)
            return capacity_order(variant), price is None, cost, type_rank(variant), position
        return [variant for _, variant in sorted(enumerate(eligibility.variants), key=order)]

    def launch(self, folder: Path, request: dict[str, object], deployment: dict[str, object]) -> str | CloudDeclined:
        attempt = ledger.text(request, 'attempt')
        self.book.require_gate_launchable(attempt)
        path = folder / 'launch.json'
        launch: dict[str, object] = read(path) if path.exists() else {}
        if launch.get('instance'):
            self.box_region = str(launch.get('region', self.api.region))
            self.box_zone = str(launch.get('zone', ''))
            return ledger.text(launch, 'instance')
        sizes = settings().words('instance_types')
        deployed = run_cost.deployed_gate_variants(deployment, sizes)
        if 'instance_types' in deployment:
            if deployed.unsupported_types:
                print(f'Cloud gate skips {" ".join(deployed.unsupported_types)}: the stack does not allow it. Run deploy.py up.',
                      flush=True)
            if len(deployed.unsupported_types) == len(sizes) and not path.exists():
                self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateAbsent(int(self.clock())))
                return CloudDeclined('The stack allows no configured gate type; run deploy.py up')
        variants: Sequence[GateLaunchVariant] = deployed.variants
        if not path.exists():
            if 'retry_launch' in request:
                launch = ledger.mapping(request['retry_launch'])
            else:
                readings = self.live_spot_prices(variants)
                live_prices = [price for reading in readings if reading.used for price in reading.prices]
                variants = self.rank_variants(variants, gate.SuiteMode.step(ledger.text(request, 'mode')), live_prices)
                if not variants:
                    self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateAbsent(int(self.clock())))
                    return CloudDeclined('Every gate type and region has a median gate time above max_gate_minutes')
                launch = {'variant': 0, 'variants': [asdict(row) for row in variants],
                    'spot_price_reads': [dict(region=row.region, seconds=row.seconds, used=row.used) for row in readings]}
            save(path, launch)
        if 'variants' in launch:
            variants = [GateLaunchVariant(ledger.text(row, 'region'), ledger.text(row, 'instance_type'),
                ledger.text(row, 'subnet_id'), ledger.text(row, 'zone')) for row in ledger.rows(launch, 'variants')]
        saved_order = {key: launch[key] for key in ('variants', 'spot_price_reads', 'unauthorized_types') if key in launch}
        unauthorized = list(cast(list[str], launch.get('unauthorized_types', [])))
        self.price_read_evidence = ledger.rows(launch, 'spot_price_reads') if 'spot_price_reads' in launch else ()
        regional_rows = {ledger.text(row, 'region'): row for row in ledger.rows(deployment, 'regions')}
        index = ledger.instant(launch, 'variant') if launch else 0
        stop = max(index + 1, len(variants)) if launch.get('arguments') else len(variants)
        unreachable: set[str] = set()
        for offset in range(index, stop):
            replay = bool(launch.get('arguments')) and offset == index
            fresh_assignment = not replay and request.get('placement_claim') is not None
            if replay:
                arguments = ledger.mapping(launch['arguments'])
                self.box_region = str(launch.get('region', self.api.region))
                size = ledger.text(arguments, 'InstanceType')
                zone = str(launch.get('zone', ''))
            else:
                variant = variants[offset]
                if variant.instance_type in unauthorized or variant.region in unreachable:
                    continue
                regional = regional_rows[variant.region]
                size = variant.instance_type
                subnet = {'id': variant.subnet_id, 'zone': variant.zone}
                self.box_region = ledger.text(regional, 'region')
                zone = variant.zone
                lifetime = max(1, (ledger.instant(request, 'expires') - int(self.clock()) + 59) // 60)
                prefix = 'artifacts/' + ledger.text(request, 'attempt')
                links = {key: ledger.text(request, key) for key in ('attempt', 'commit', 'tree', 'mode', 'origin')}
                vcpus = int(size.split('.')[1].removesuffix('xlarge')) * 4
                budget = (gate_record.NoGateTimeLimit('paired trial') if request.get('task') == 'paired'
                          else gate_record.NoGateTimeLimit('named modules') if request.get('named_modules') is True
                          else gate.read_time_limit(vcpus, cast(list[str], request['modules']), vcpus,
                              gate.SuiteMode(str(request['mode'])) is gate.SuiteMode.TARGETED and not request.get('full', False)))
                seconds = budget.seconds if isinstance(budget, gate_record.GateTimeLimit) else 0.0
                links['gate_time_limit_s'] = str(seconds)
                credentials = self.api.call('export-credentials', '--format', 'process', service='configure')
                signing = s3_presign.SigningKey(ledger.text(credentials, 'AccessKeyId'), ledger.text(credentials, 'SecretAccessKey'),
                    ledger.text(credentials, 'SessionToken') if 'SessionToken' in credentials else '')
                at = int(self.clock())
                def url(method: str, key: str) -> str:
                    return s3_presign.presign(method, ledger.text(deployment, 'bucket') + '.s3.' + self.api.region + '.amazonaws.com',
                        key, self.api.region, signing, datetime.fromtimestamp(at, UTC), max(1, ledger.instant(request, 'expires') - at))
                links.update(production='true', step='size', expires=str(request['expires']),
                             guest=url('GET', 'requests/' + ledger.text(request, 'attempt') + '.guest.py'),
                             request=url('GET', 'requests/' + ledger.text(request, 'attempt') + '.json'),
                             log=url('PUT', prefix + '/gate.log'), result=url('PUT', prefix + '/result.json'),
                             boot_error=url('PUT', prefix + '/boot-error.json'))
                if request['source_key']:
                    links['source'] = url('GET', ledger.text(request, 'source_key'))
                if request.get('task') == 'paired':
                    links['paired'] = 'true'
                policy = read_settings(self.settings_path)
                tags = [{'Key': 'GateDeployment', 'Value': ledger.text(deployment, 'deployment_id')},
                        {'Key': 'Workload', 'Value': 'cloud-claude-gate'}, {'Key': 'Name', 'Value': 'cloud-claude-gate'}, {'Key': 'GateAttempt', 'Value': ledger.text(request, 'attempt')},
                        {'Key': 'ExpiresAt', 'Value': str(request['expires'])},
                        {'Key': policy.cost_explorer_tag_key, 'Value': policy.cost_explorer_tag_value}]
                arguments: dict[str, object] = {
                    'LaunchTemplate': {'LaunchTemplateId': regional['template'], 'Version': regional['version']},
                    'MinCount': 1, 'MaxCount': 1, 'ClientToken': ledger.text(request, 'attempt') + '-' + str(offset),
                    'InstanceType': size,
                    'NetworkInterfaces': [{'DeviceIndex': 0, 'SubnetId': subnet['id'],
                                           'Groups': [regional['security_group']], 'AssociatePublicIpAddress': True}],
                    'TagSpecifications': [{'ResourceType': kind, 'Tags': tags} for kind in ('instance', 'volume')],
                    'UserData': gate_box.user_data({**links, 'instance_type': size}, ledger.text(deployment, 'revision'), lifetime,
                              settings().number('log_push_s'), float(str(request['module_time_limit_s'])))}
                if regional.get('image'):
                    arguments['ImageId'] = ledger.text(regional, 'image')
                launch = {**saved_order, 'variant': offset, 'arguments': arguments, 'region': self.box_region, 'zone': zone,
                          'gate_time_limit_s': seconds}
                if not fresh_assignment:
                    save(path, launch)
            api = self.regional_api(self.box_region)
            self.book.gate_region(attempt, self.box_region)
            self.size_vcpus = int(ledger.text(arguments, 'InstanceType').split('.')[1].removesuffix('xlarge')) * 4
            if not fresh_assignment and self.clock() >= ledger.instant(request, 'expires'):
                reply = api.call('describe-instances', '--filters',
                                      'Name=client-token,Values=' + ledger.text(arguments, 'ClientToken'))
                instances = [instance for group in ledger.rows(reply, 'Reservations')
                             for instance in ledger.rows(group, 'Instances')]
                if len(instances) == 1:
                    instance = ledger.text(instances[0], 'InstanceId')
                    save(path, {**launch, 'instance': instance})
                    self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateRunning(instance), instance_type=size)
                    self.box_zone = zone
                    return instance
                raise AwsUnavailable('The expired launch remains unconfirmed')
            launch_json = json.dumps(arguments)
            try:
                for retry in range(settings().whole('launch_reply_tries')):
                    try:
                        if retry == 0:
                            cloud_progress.publish(self.sidecar, cloud_progress.CloudProgress(
                                'requesting', self.size_vcpus, 0, self.clock(), stage='launch'))
                            if fresh_assignment:
                                if reason := self._check_launch_booking(attempt):
                                    return CloudDeclined(reason)
                                # Save the token before AWS can accept the request.
                                save(path, launch)
                        self.book.require_gate_launchable(attempt)
                        reply = api.call('run-instances', '--cli-input-json', launch_json)
                        self.record_launch(request, size, zone, 'launched')
                        break
                    except AwsUnavailable as error:
                        self.record_launch(request, size, zone, 'refused', str(error))
                        if not aws_down(str(error)) or retry + 1 == settings().whole('launch_reply_tries'):
                            raise
                    except subprocess.SubprocessError as error:
                        self.record_launch(request, size, zone, 'refused',
                                           type(error).__name__ + ': ' + str(error))
                        raise
                else:
                    raise AssertionError('The launch retry count must be positive')
            except AwsUnavailable as error:
                # A saved request keeps its token and bytes after an uncertain reply.
                if capacity_error(str(error)):
                    launch = {**saved_order, 'variant': offset + 1}
                    save(path, launch)
                    continue
                if not_sent(str(error)) and any(row.region != self.box_region for row in variants[offset + 1:]):
                    # No box exists, so the next region can launch; this region's other variants would fail the same way.
                    print(f'Cloud gate skips {self.box_region}: {launch_error_code(str(error)) or str(error)}', flush=True)
                    unreachable.add(self.box_region)
                    launch = {**saved_order, 'variant': offset + 1}
                    save(path, launch)
                    continue
                if launch_error_code(str(error)) == 'UnauthorizedOperation' and any(
                        row.instance_type not in (size, *unauthorized) for row in variants[offset + 1:]):
                    # The stack policy names its types, so another type can still launch.
                    unauthorized.append(size)
                    saved_order['unauthorized_types'] = unauthorized
                    launch = {**saved_order, 'variant': offset + 1}
                    save(path, launch)
                    continue
                if not definite_refusal(str(error)):
                    raise
                self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateAbsent(int(self.clock())))
                return CloudDeclined(f'{size} in {zone}: {launch_error_code(str(error))}')
            instances = ledger.rows(reply, 'Instances')
            if len(instances) != 1:
                raise AwsUnavailable('The launch reply has no unique instance')
            instance = ledger.text(instances[0], 'InstanceId')
            save(path, {**launch, 'instance': instance})
            self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateRunning(instance), instance_type=size)
            self.box_zone = zone
            return instance
        self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateAbsent(int(self.clock())))
        return CloudDeclined('Spot capacity is unavailable in every configured region')

    def artifact_etags(self, bucket: str, prefix: str) -> dict[str, str]:
        listing = self.api.call('list-objects-v2', '--bucket', bucket, '--prefix', prefix,
                                '--no-paginate', service='s3api')
        return {ledger.text(row, 'Key'): ledger.text(row, 'ETag')
                for row in ledger.rows({'Contents': listing.get('Contents', [])}, 'Contents')}

    def _check_launch_booking(self, attempt: str) -> str:
        """Close the booking if the current cap blocks launch."""
        now = int(self.clock())
        reason = self.book.recheck_gate_booking(self.settings_path, attempt, now)
        if reason:
            self.book.resolve_gate(attempt, ledger.GateAbsent(now))
        return reason

    def execute(self, folder: Path, request: dict[str, object], deployment: dict[str, object]) -> dict[str, object]:
        attempt = ledger.text(request, 'attempt')
        row: dict[str, object] = {}
        interruption_code = ''
        interruption_confirmed = False
        result_seen_at: int | ledger.GateResultNotReceived = ledger.GATE_RESULT_NOT_RECEIVED
        try:
            # Read the wait before any launch, so a bad value costs no box.
            unreachable_wait_s = target_config.unreachable_wait_seconds(conf.read_or_repository('build_targets'))
            self.reconcile()
            previous = next((row for row in self.book.read_snapshot().gate_reservations if row.attempt == attempt), None)
            if previous is not None and isinstance(previous.state, (ledger.GateAbsent, ledger.GateTerminated)):
                return {'status': 'fallback', 'reason': 'The saved attempt has ended'}
            claim = request.get('placement_claim')
            if claim is not None and (previous is None or previous.placement_claim != claim):
                return {'status': 'fallback', 'reason': 'The worker has no matching placement booking'}
            new_launch = not (folder / 'launch.json').exists()
            if new_launch and claim is None:
                if request.get('task') != 'paired':
                    self.charges()
                reservation = self.book.reserve_gate(self.settings_path, gate_booking(
                    settings(), attempt, int(self.clock()), ledger.instant(request, 'expires')))
                if isinstance(reservation, (ledger.GateCapReached, ledger.GateAttemptEnded)):
                    return {'status': 'fallback', 'reason': reservation.reason}
            identity = process_start(os.getpid())
            if not isinstance(identity, Started):
                raise ValueError('The worker has no readable process identity')
            owner = ledger.GateWorkerOwner(os.getpid(), identity.start, str(folder.resolve()))
            self.book.gate_owner(attempt, owner, now=self.clock())
            if new_launch:
                if claim is not None:
                    self.charges()
                    if reason := self._check_launch_booking(attempt):
                        return {'status': 'fallback', 'reason': reason}
                bucket = ledger.text(deployment, 'bucket')
                if request['source_key']:
                    self.upload(bucket, ledger.text(request, 'source_key'), folder / 'source.bundle')
                self.upload(bucket, 'requests/' + attempt + '.json', folder / 'request.json')
                guest = folder / 'guest.py'
                _ = guest.write_text(gate_box.GUEST)
                self.upload(bucket, 'requests/' + attempt + '.guest.py', guest)
            launched = self.launch(folder, request, deployment)
            if isinstance(launched, CloudDeclined):
                return {'status': 'fallback', 'reason': launched.reason}
            instance = launched
            prefix = 'artifacts/' + attempt + '/'
            log_key = prefix + 'gate.log'
            boot_error_key = prefix + 'boot-error.json'
            result_key = prefix + 'result.json'
            try:
                arguments = ledger.mapping(read(folder / 'launch.json')['arguments'])
                self.size_vcpus = int(ledger.text(arguments, 'InstanceType').split('.')[1].removesuffix('xlarge')) * 4
                cloud_progress.publish(self.sidecar, cloud_progress.CloudProgress('requesting', self.size_vcpus, 0, self.clock(),
                    stage='launch', region=self.box_region, zone=self.box_zone))
                limit = float(str(read(folder / 'launch.json').get('gate_time_limit_s', 0)))
                if not limit and request.get('task') != 'paired':
                    print(f'Gate time limit disabled: no passing history for {self.size_vcpus} vCPUs', flush=True)
                began = 0.0
                deadline = float('inf')
                ec2_outage = _PollOutage()
                s3_outage = _PollOutage()
                bucket = ledger.text(deployment, 'bucket')
                downloaded_log_etag = ''
                instance_check_s = settings().whole('instance_check_s')
                next_instance_check = time.monotonic()
                # The box pushes gate.log every log_push_s; no new log for unreachable_wait_s means it is unreachable.
                # A paired trial pauses the pushes while it measures, so only the EC2 state shows that it lives.
                watch_silence = request.get('task') != 'paired'
                seen_log_etag = ''
                last_answer = time.monotonic()
                while self.clock() < ledger.instant(request, 'expires'):
                    now = time.monotonic()
                    deadline_reached = now >= deadline
                    stop_reason = ''
                    stop_code = ''
                    silent_s = 0.0
                    ec2_error: AwsUnavailable | None = None
                    if deadline_reached or now >= next_instance_check:
                        next_instance_check = now + instance_check_s
                        try:
                            regional_api = self.regional_api(self.box_region)
                            state = regional_api.observe(instance, int(self.clock())).state
                            if state not in ('pending', 'running'):
                                reply = regional_api.call('describe-instances', '--instance-ids', instance)
                                stopped = ledger.rows(ledger.rows(reply, 'Reservations')[0], 'Instances')[0]
                                reason = ledger.mapping(stopped.get('StateReason', {}))
                                stop_code = str(reason.get('Code', ''))
                                detail = ': '.join(ledger.text(reason, key) for key in ('Code', 'Message') if reason.get(key))
                                stop_reason = 'The instance stopped without a final result' + (f' ({detail})' if detail else '')
                            ec2_outage.reset()
                        except subprocess.TimeoutExpired:
                            pass
                        except AwsUnavailable as error:
                            if not ec2_outage.retry_after_failure(error, self.clock()):
                                ec2_error = error
                    try:
                        etags = self.artifact_etags(bucket, prefix)
                        log_etag = etags.get(log_key, '')
                        log_changed = bool(log_etag and log_etag != downloaded_log_etag)
                        if log_etag != seen_log_etag:
                            seen_log_etag = log_etag
                            last_answer = now
                        elif watch_silence:
                            silent_s = now - last_answer
                        if boot_error_key in etags and self.download(bucket, boot_error_key, folder / 'boot-error.json'):
                            reason = ledger.text(read(folder / 'boot-error.json'), 'error')
                            if log_changed:
                                try:
                                    if self.download(bucket, log_key, folder / 'gate.log'):
                                        self.publish_log(folder)
                                except subprocess.TimeoutExpired:
                                    pass
                            return {'status': RetryableCloudFailure.BOX.value, 'reason': reason}
                        if result_key in etags and self.download(bucket, result_key, folder / RESULT_FILE):
                            received_at = int(self.clock())
                            result = read(folder / RESULT_FILE)
                            try:
                                validate_result(result, request, instance=instance)
                            except LogUploadFailed as error:
                                return {'status': RetryableCloudFailure.BOX.value, 'reason': str(error)}
                            if not self.download(bucket, log_key, folder / 'gate.log'):
                                raise ValueError('The final log is absent')
                            if request.get('task') == 'paired' and any(
                                    sample['instance_type'] != arguments['InstanceType'] for sample in ledger.rows(result, 'runs')):
                                raise ValueError('The sample instance type differs from the launch')
                            if request.get('task') == 'paired':
                                # Validation names the box in each sample; the outcome reads this file.
                                save(folder / RESULT_FILE, result)
                            result_seen_at = received_at
                            self.record_launch(request, ledger.text(arguments, 'InstanceType'), '', 'finished',
                                               gate_s=float(str(result['gate_s'])))
                            row = result
                            self.publish_log(folder)
                            break
                        if result_key not in etags and log_changed and self.download(bucket, log_key, folder / 'gate.log'):
                            downloaded_log_etag = log_etag
                            self.publish_log(folder)
                            if not began and 'Runner: ' in (folder / 'gate.log').read_text():
                                began = time.monotonic()
                                deadline = began + limit if limit else float('inf')
                        s3_outage.reset()
                    except subprocess.TimeoutExpired:
                        # Time when S3 cannot be read is not silence from the box.
                        last_answer = now
                    except AwsUnavailable as error:
                        last_answer = now
                        if not s3_outage.retry_after_failure(error, self.clock()):
                            raise
                    if ec2_error is not None:
                        raise ec2_error
                    if stop_reason:
                        if stop_code in SPOT_INTERRUPTION_CODES:
                            interruption_code = stop_code
                        raise ValueError(stop_reason)
                    if silent_s >= unreachable_wait_s:
                        raise ValueError(f'unreachable: the box sent no log for {silent_s:.0f} seconds '
                                         + f'({target_config.UNREACHABLE_WAIT_KEY}={unreachable_wait_s})')
                    if deadline_reached:
                        text = (folder / 'gate.log').read_text() if (folder / 'gate.log').exists() else ''
                        row = time_limit_result(request, text, time.monotonic() - began, limit, self.size_vcpus)
                        save(folder / RESULT_FILE, row)
                        with (folder / 'gate.log').open('a') as output:
                            _ = output.write('\n' + gate_box.decode_run(row['gate']).time_limit_report + '\n')
                        break
                    self.pause(settings().number('result_poll_s'))
                if not row:
                    raise ValueError('The instance expired without a final result')
            finally:
                with cancellation.defer_cleanup_signals():
                    self.terminate(attempt, instance, owner, result_seen_at=result_seen_at)
                    interruption_confirmed = bool(interruption_code)
            return {'status': 'complete', 'instance': instance, 'result_file': RESULT_FILE}
        except ledger.GateCleanupInProgress as error:
            return {'status': 'fallback', 'reason': str(error)}
        except (OSError, ValueError, KeyError, TypeError, AwsUnavailable, subprocess.SubprocessError) as error:
            launch = read(folder / 'launch.json') if (folder / 'launch.json').exists() else {}
            # Saved arguments mean AWS may have accepted the launch; a saved variant alone does not.
            if 'arguments' not in launch:
                with suppress(ledger.GateCleanupInProgress):
                    self.book.resolve_gate(attempt, ledger.GateAbsent(int(self.clock())))
            if row and request.get('task') != 'paired' and not gate_box.decode_run(row['gate']).passed:
                return {'status': 'complete', 'result_file': RESULT_FILE}
            if interruption_confirmed:
                return {'status': 'interrupted', 'reason': str(error), 'interruption_code': interruption_code}
            return {'status': 'fallback', 'reason': str(error)}


def time_limit_result(request: dict[str, object], text: str, elapsed: float,
                      limit: float, vcpus: int) -> dict[str, object]:
    """Keep a red result when the box does not return its own result."""
    completed = {match[1]: (int(match[2]), float(match[3])) for match in re.finditer(
        r"(?m)^(\S+) exit=(-?\d+) seconds=(\d+\.\d+)(?: stopped)?$", text)}
    started = set(re.findall(r"(?m)^Starting module: (\S+)$", text))
    running = sorted(started - completed.keys())
    report = (f"time limit: limit {limit:g} s; elapsed {elapsed:.3f} s; "
              f"running modules: {', '.join(running) or '(none)'}")
    jobs: list[gate.GateJobResult] = []
    for path in cast(list[str], request['modules']):
        name = path.removeprefix('scripts/')
        suite, module = name.split('/', 1)
        status, seconds = completed.get(name, (124, elapsed if name in started else 0.0))
        failure = re.search(r'(?ms)^--- ' + re.escape(name) + r' exit=-?\d+ ---\n(.*?)\n--- end module failure ---', text)
        output = gate.failure_log_tail(failure[1]) if failure else report if status else ''
        jobs.append(gate.GateJobResult(suite, module, status, seconds, output,
                                      killed_for=report if name not in completed else ''))
    details = [f"--- {job.suite}/{job.module} exit={job.returncode} ---\n{job.output}"
               for job in jobs if job.returncode and not job.killed_for]
    report = '\n'.join([report, *details])
    run = gate.GateRun(tuple(jobs), elapsed, 0, executions=tuple(jobs), time_limit_report=report)
    now = time.time()
    record = gate_box.completion_record(run, ledger.text(request, 'attempt'), ledger.text(request, 'commit'),
                                        ledger.text(request, 'tree'), store.host_name(), now - elapsed, now,
                                        gate.SuiteMode.step(ledger.text(request, 'mode')))
    record['vcpus'] = vcpus
    record['max_jobs'] = vcpus
    return {'attempt': request['attempt'], 'commit': request['commit'], 'tree': request['tree'],
            'host': record['host'], 'verdict': 'fail', 'gate': gate_box.encode_run(run),
            'record': record, 'gate_s': elapsed}


def outcome_result(folder: Path, outcome: dict[str, object]) -> dict[str, object]:
    """Read a complete outcome's result: its named file, or the result an older outcome holds."""
    return read(folder / RESULT_FILE) if outcome.get('result_file') == RESULT_FILE else ledger.mapping(outcome['result'])


def _outcome_imported(folder: Path) -> bool:
    # The attempt fixes the record ID. The import can remove the result file.
    try:
        return read(folder / 'imported.json').get('id') == 'gate-' + folder.name
    except (FileNotFoundError, ValueError):
        return False


def decode_requested_run(row: dict[str, object], request: dict[str, object]) -> gate.GateRun:
    """Decode a result for the requested attempt and source tree."""
    for key in ('attempt', 'commit', 'tree'):
        if row.get(key) != request[key]:
            raise ValueError('The result differs from the current ' + key)
    if row.get('log_error'):
        raise LogUploadFailed('The final log upload failed')
    return gate_box.decode_run(row['gate'])


def validate_run_modules(run: gate.GateRun, request: dict[str, object]) -> None:
    modules = ['scripts/' + job.suite + '/' + job.module for job in run.jobs]
    if (any(job.kind is not gate.GateJobKind.TEST_MODULE for job in run.jobs)
            or len(modules) != len(set(modules)) or set(modules) != set(cast(list[str], request['modules']))):
        raise ValueError('The result has a different module list')


def validate_result(row: dict[str, object], request: dict[str, object], *, instance: str = '') -> None:
    if request.get('task') == 'paired':
        gate_box.validate_paired_result(row, request, instance)
        return
    run = decode_requested_run(row, request)
    if row.get('verdict') != ('pass' if run.passed else 'fail'):
        raise ValueError('The result has no complete verdict and log')
    validate_run_modules(run, request)
    host = ledger.text(row, 'host')
    if not host or Path(host).name != host or host in ('.', '..'):
        raise ValueError('Invalid remote host')
    record = ledger.mapping(row['record'])
    if (record.get('id') != 'gate-' + ledger.text(request, 'attempt') or record.get('tree') != request['tree']
            or record.get('sha') != request['commit'] or record.get('host') != row.get('host')
            or record.get('kind') != 'gate_run' or record.get('status') != int(not run.passed)):
        raise ValueError('The buildlog record differs from the result')


def _import_already_appended(marker: Path, handle: BinaryIO, destination: Path, record_id: str) -> bool:
    """Search the bytes that another import could have appended for this record."""
    try:
        saved = read(marker)
    except FileNotFoundError:
        return False
    if saved.get('id') != record_id or saved.get('destination') != str(destination):
        return False
    offset = saved.get('offset')
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError('The gate import marker has an invalid file offset')
    _ = handle.seek(offset)
    with open(handle.fileno(), 'rb', closefd=False) as tail:
        for line in tail:
            try:
                previous = cast(object, json.loads(line))
            except ValueError:
                continue
            if isinstance(previous, dict) and cast(dict[str, object], previous).get('id') == record_id:
                return True
    return False


def import_record(folder: Path, row: dict[str, object]) -> None:
    record = ledger.mapping(row['record'])
    host = ledger.text(record, 'host')
    if not host or Path(host).name != host or host in ('.', '..'):
        raise ValueError('Invalid remote host')
    at = datetime.fromisoformat(ledger.text(record, 'started_at')).timestamp()
    record_id = ledger.text(record, 'id')
    # The record keeps the box host; the file is the one AWS place.
    destination = store.host_file(store.CLOUD_GATE_PLACE, at).absolute()
    marker = folder / 'import-boundary.json'
    with store.locked_append(destination) as handle:
        # The lock repairs a partial row before it gives us the offset.
        offset = handle.tell()
        if not _import_already_appended(marker, handle, destination, record_id):
            save(marker, {'id': record_id, 'destination': str(destination), 'offset': offset})
            # An older box names the step merge-gate or targeted-test.
            record = {**record, 'step': gate.SuiteMode.step(ledger.text(record, 'step'))}
            run = gate_box.decode_run(row['gate'])
            gate_record.keep_failure_logs(record_id, store.CLOUD_GATE_PLACE, at,
                cast(list[gate_record.ModuleRun], record['modules']), gate_box.execution_records(run))
            store.append_locked(handle, record)
    from scripts.buildlog.record import keep_result
    imported: dict[str, object] = {'id': record_id}
    try:
        # The result leaves the checkout: a gate copy is a reused slot.
        imported['result'] = keep_result(json.dumps(row).encode(), store.CLOUD_GATE_PLACE, at, record_id)
    except OSError as error:
        gate_record.report_not_written(error)
    save(folder / 'imported.json', imported)
    if 'result' in imported:
        try:
            (folder / RESULT_FILE).unlink(missing_ok=True)
        except OSError as error:
            gate_record.report_not_written(error)


@dataclass(frozen=True)
class MissingGateIndex:
    """The checkout has no index file."""


@dataclass(frozen=True)
class GateIndexStamp:
    device: int
    inode: int
    changed_ns: int
    modified_ns: int

    @classmethod
    def read(cls, path: Path) -> GateIndexStamp | MissingGateIndex:
        try:
            info = path.stat()
        except FileNotFoundError:
            return MissingGateIndex()
        return cls(info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns)


@contextmanager
def stable_gate_index(source: Path) -> Generator[Path]:
    def contents(path: Path) -> bytes:
        with suppress(FileNotFoundError):
            return path.read_bytes()
        return b''

    with tempfile.TemporaryDirectory(prefix='cloud-gate-index-') as temporary:
        index = Path(temporary) / 'index'
        stamp = GateIndexStamp.read(source)
        # Keep the stat cache and its timestamp for Git's racy-entry check.
        with suppress(FileNotFoundError):
            git_index.private_copy(source, index)
        copied = contents(index)
        yield index
        if contents(source) != copied or GateIndexStamp.read(source) != stamp:
            raise GateRefused('The Git index changed during the checkout read')


def working_tree(repo: Path) -> tuple[str, set[str]]:
    head, index_path = gate.checked_git(
        repo, 'rev-parse', '--path-format=absolute', 'HEAD^{tree}', '--git-path', 'index').split('\n', 1)
    routed = gate.routed_gate_tree(repo)
    bound = routed.tree if isinstance(routed, gate.RoutedGateTree) else head

    def git(index: Path, *arguments: str, input: bytes = b'') -> bytes:
        environment = {**os.environ, 'GIT_INDEX_FILE': str(index)}
        return subprocess.run(['git', *arguments], cwd=repo, env=environment, input=input,
                              check=True, capture_output=True).stdout

    with stable_gate_index(Path(index_path)) as index:
        index_changes = git(index, 'diff-index', '--cached', '--no-renames', '--ignore-submodules=none',
                            '--name-status', '-z', head)
        files = git(index, 'ls-files', '-v', '-z', '--cached', '--modified', '--others', '--exclude-standard')
        states = {entry[:1] for entry in files.split(b'\0') if entry}
        # H marks tracked entries. Other tags can indicate changes or skipped checks.
        if bound == head and not index_changes and states <= {b'H'}:
            return head, set()
        staged = index_changes.split(b'\0')
        skipped = any(state == b'S' or state.islower() for state in states)
        if bound != head or b'D' in staged[::2] or skipped:
            # Keep removed paths and read files that the copied index marks to skip.
            entries = git(index, 'ls-files', '--stage', '-z')
            index = index.with_name('working')
            _ = git(index, 'read-tree', bound, head)
            _ = git(index, 'update-index', '--replace', '-z', '--index-info', input=entries)
        _ = git(index, 'add', '-A')
        tree = git(index, 'write-tree').decode('ascii').strip()
        working_changes = (git(index, 'diff-tree', '--no-renames', '--ignore-submodules=none', '--name-only', '-r',
                               '-z', head, tree).split(b'\0') if tree != head else [])
    dirty = {path.decode('utf-8', errors='replace') for path in (*staged[1::2], *working_changes)}
    dirty.discard('')
    return tree, dirty


def create_source_bundle(repo: Path, bundle: Path, commits: Sequence[str], *, exclude: str = '') -> None:
    """Write one bundle for the requested tips and an optional common base."""
    refs: list[str] = []
    try:
        for index, commit in enumerate(dict.fromkeys(commits)):
            suffix = f'-{index}' if index else ''
            ref = f'refs/cloud-gates/{bundle.parent.name}{suffix}'
            refs.append(ref)
            _ = gate.checked_git(repo, 'update-ref', ref, commit)
        prerequisite = ('^' + exclude,) if exclude else ()
        _ = gate.checked_git(repo, 'bundle', 'create', str(bundle), *refs, *prerequisite)
    finally:
        for ref in refs:
            _ = gate.checked_git(repo, 'update-ref', '-d', ref)


def require_selected_tree(repo: Path, tree: str, selected: gate.AllModules | gate.TreeSelection) -> None:
    if ((isinstance(selected, gate.TreeSelection) and tree != selected.tree)
            or not gate.matches_routed_gate_tree(repo, tree)):
        raise GateRefused('The checkout changed after the test selection')


def request_test_modules(repo: Path, tree: str, selected: gate.AllModules | gate.TreeSelection) -> list[str]:
    listing = gate.checked_git(repo, 'ls-tree', '-rz', '--full-tree', tree, '--', 'scripts/')
    modules: list[str] = []
    for name, entry in parse_tree_listing(listing).items():
        path = Path(name)
        if entry.kind != 'blob' or not is_test_module(name) or '__pycache__' in path.parts[:-1]:
            continue
        if len(path.parts) < 3:
            raise GateRefused(f'test module must be in a suite folder under scripts/: {path.name}')
        modules.append(name)
    if isinstance(selected, gate.TreeSelection):
        paths = set(selected.paths)
        if not paths <= set(modules):
            raise GateRefused('selected modules are absent from the tested tree')
        modules = [name for name in modules if name in paths]
    return sorted(modules, key=lambda name: Path(name).parts)


def prepare_request(repo: Path, folder: Path, mode: gate.SuiteMode, limit: float, *,
                    selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES) -> dict[str, object]:
    head = gate.checked_git(repo, 'rev-parse', 'HEAD')
    tree, dirty = working_tree(repo)
    require_selected_tree(repo, tree, selected)
    if not dirty and gate.checked_git(repo, 'rev-parse', f'{head}^{{tree}}') != tree:
        raise GateRefused('The checkout changed during cloud request preparation')
    commit = gate.checked_git(repo, 'commit-tree', tree, '-p', head, '-m', 'Cloud gate snapshot') if dirty else head
    modules = request_test_modules(repo, tree, selected)
    origin_main = gate.checked_git(repo, 'rev-parse', 'refs/remotes/origin/main')
    origin = gate.checked_git(repo, 'remote', 'get-url', 'origin')
    from urllib.parse import urlsplit
    if urlsplit(origin).password or urlsplit(origin).query or ('://' in origin and urlsplit(origin).username):
        raise GateRefused('The origin URL must contain no credential')
    folder.mkdir(parents=True, exist_ok=True)
    request: dict[str, object] = {'attempt': folder.name, 'commit': commit, 'head': head, 'tree': tree, 'dirty_paths': sorted(dirty), 'origin': origin,
        'origin_main': origin_main, 'clone_url': settings().text('clone_url'),
        'mode': mode.wire, 'modules': modules, 'source_key': '',
        'expires': int(time.time()) + settings().whole('lifetime_minutes') * 60,
        'module_time_limit_s': min(limit, settings().number('module_time_limit_s'))}
    return request


class IncompatibleSavedGateRequest(ValueError):
    """The saved request cannot run the selected modules."""


def read_saved_request(repo: Path, path: Path, tree: str,
                       selected: gate.AllModules | gate.TreeSelection) -> dict[str, object]:
    request = read(path)
    if request.get('attempt', path.parent.name) != path.parent.name:
        raise ValueError('The saved request differs from the current cloud attempt')
    if request.get('modules') != request_test_modules(repo, tree, selected):
        raise IncompatibleSavedGateRequest('The saved request has a different module list')
    return request


def gate_attempt_command(operation: Literal['--worker', '--cleanup'], folder: Path,
                         directory: Path, sidecar: Path) -> list[str]:
    return service_command('scripts.aws_target.cloud_gate',
        [operation, str(folder), '--directory', str(directory), '--sidecar', str(sidecar),
         '--settings', str(account_settings_path())], 'cloud-gate-' + folder.name)


def reconcile_command(directory: Path, *, start_only: bool = False) -> list[str]:
    """Use one unit name so only one reconcile service runs at a time."""
    return service_command('scripts.aws_target.cloud_gate',
        ['--reconcile', '--directory', str(directory), '--settings', str(account_settings_path())],
        'cloud-gate-reconcile', start_only=start_only)


def _start_reconcile_if_due(directory: Path, rows: Sequence[ledger.GateReservation],
                            service: Callable[[Sequence[str]], int]) -> None:
    now = int(time.time())
    if any(cleanup_due(row, now) for row in rows):
        _ = service(reconcile_command(directory))


def start_reconcile_service(command: Sequence[str], timeout: float, *, existing_unit: str = '') -> None:
    """Submit a service and accept an existing submission of the named unit."""
    # The admission service can run without a user session environment.
    runtime = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
    result = subprocess.run(command, env={**os.environ, 'XDG_RUNTIME_DIR': runtime, 'LC_ALL': 'C'}, check=False,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        detail = result.stderr.strip()
        if existing_unit and detail == (f'Failed to start transient service unit: Unit {existing_unit}.service '
                                        + 'was already loaded or has a fragment file.'):
            return
        raise OSError(detail or f'systemd-run exited with status {result.returncode}')


_reconcile_start_warnings: set[tuple[str, int]] = set()
_reconcile_start_warnings_lock = Lock()


def reconcile_expired(episode: str, attempt: int) -> None:
    """Request reconciliation for an expired claim when cleanup is due."""
    try:
        directory, rows = _claim_bookings(episode, attempt)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        return
    try:
        now = int(time.time())
        if not any(row.expires <= now and cleanup_due(row, now) for row in rows):
            return
        command = reconcile_command(directory, start_only=True)
        timeout = settings().number('api_timeout_s')
        _, current = _claim_bookings(episode, attempt)
        if not any(row.expires <= now and ledger.gate_is_open(row) for row in current):
            return
        start_reconcile_service(command, timeout)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, subprocess.SubprocessError) as error:
        key = (episode, attempt)
        with _reconcile_start_warnings_lock:
            if key in _reconcile_start_warnings:
                return
            _reconcile_start_warnings.add(key)
        detail = ' '.join(str(error).split())
        print(f'The cloud gate reconcile did not start: {detail}', file=sys.stderr, flush=True)


def end_open_gates(directory: Path, service: Callable[[Sequence[str]], int]) -> None:
    """Start reconciliation for gates due for cleanup when the switch is off."""
    try:
        book = ledger.CostLedger(directory)
        if book.path.exists():
            _start_reconcile_if_due(directory, book.read_snapshot().gate_reservations, service)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f'The cloud gate reconcile did not start: {error}', file=sys.stderr, flush=True)


def settle_incompatible_attempt(folder: Path, book: ledger.CostLedger,
                                service: Callable[[Sequence[str]], int]) -> bool:
    def settled(*, cleanup_finished: bool) -> bool:
        rows = book.read_snapshot().gate_reservations if book.path.exists() else ()
        for row in rows:
            if row.attempt == folder.name:
                return not ledger.gate_is_open(row)
        return cleanup_finished and not (folder / 'launch.json').exists()

    if settled(cleanup_finished=False):
        return True
    # Use the old unit name so systemd refuses cleanup while its worker runs.
    command = gate_attempt_command('--cleanup', folder, book.directory, folder / 'reservation.cgroup')
    return service(command) == 0 and settled(cleanup_finished=True)


def prepare(repo: Path, folder: Path, mode: gate.SuiteMode, limit: float, *,
            selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES,
            gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED) -> dict[str, object]:
    request = prepare_request(repo, folder, mode, limit, selected=selected)
    commit, base = (ledger.text(request, key) for key in ('commit', 'origin_main'))
    if int(gate.checked_git(repo, 'rev-list', '--count', commit, '--not', base)):
        bundle = folder / 'source.bundle'
        create_source_bundle(repo, bundle, (commit,), exclude=base)
        digest = sha256(bundle.read_bytes()).hexdigest()
        request['source_key'] = 'source/' + digest + '.bundle'
    if isinstance(gate_job, StepJob):
        request['gate_job'] = snapshot_codec.snapshot_value(gate_job)
    save(folder / 'request.json', request)
    return request


def accepts_suite_mode(repo: Path, commit: str, mode: gate.SuiteMode) -> bool:
    """Read the tested enum without executing code from the tested commit.

    The commit must read the wire value that prepare sends, as a member value or in its _missing_ mapping.
    """
    for path in ('scripts/production/gate_routes.py', 'scripts/production/gate.py', 'scripts/production/merge_gate.py'):
        source = subprocess.run(['git', 'show', f'{commit}:{path}'], cwd=repo,
                                capture_output=True, text=True, check=False)
        if source.returncode:
            continue
        try:
            module = ast.parse(source.stdout)
        except SyntaxError:
            return False
        for definition in module.body:
            if isinstance(definition, ast.ClassDef) and definition.name == 'SuiteMode':
                return any(isinstance(row, (ast.Assign, ast.AnnAssign)) and isinstance(row.value, ast.Constant)
                           and row.value.value == mode.wire
                           or isinstance(row, ast.FunctionDef) and row.name == '_missing_'
                           and any(isinstance(node, ast.Constant) and node.value == mode.wire for node in ast.walk(row))
                           for row in definition.body)
    return False


def run(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
        selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES,
        gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED,
        service: Callable[[Sequence[str]], int] = lambda command: subprocess.call(command),
        authorization: wire.FirstAuthorization | placement.NoLaunchPermission = NO_ASSIGNED_LAUNCH,
        ) -> CloudDeclined | CloudCompleted:
    # Record the final fallback here so worker replies and service errors produce one row.
    attempt = CloudAttempt(target_config.ssh_target_state_directory('aws'), mode, gate_job=gate_job)
    if isinstance(authorization, wire.FirstAuthorization):
        attempt.claim = authorization.claim
    try:
        outcome = run_cloud(repo, mode=mode, full=full, module_time_limit_s=module_time_limit_s,
                            selected=selected, service=service, attempt=attempt)
    except (OSError, ValueError, KeyError, TypeError, AwsUnavailable, subprocess.SubprocessError,
            GateRefused, affected_modules.GraphError) as error:
        outcome = CloudDeclined(str(error))
    except BaseException:
        if isinstance(authorization, wire.FirstAuthorization):
            state = finish_assigned_run(authorization, attempt, placement.AttemptCancelled('The gate caller ended'))
            if isinstance(state, placement.Pending):
                print(state.reason, file=sys.stderr, flush=True)
        raise
    if isinstance(outcome, CloudDeclined):
        if attempt.interruptions:
            outcome = CloudDeclined(f'Spot interruption on {attempt.interruptions} attempts; ' + outcome.reason)
        try:
            append_launch(attempt.directory, {'time': time.time(), 'attempt': attempt.identifier,
                'instance_type': '', 'vcpus': 0, 'result': 'fallback', 'mode': attempt.mode.value, 'region': '',
                'availability_zone': '', 'error_code': launch_error_code(outcome.reason), 'reason': outcome.reason},
                gate_job=attempt.gate_job)
        except OSError as error:
            detail = ' '.join(str(error).split())
            print('Cloud gate fallback row was not written: ' + detail, file=sys.stderr, flush=True)
    if isinstance(authorization, wire.FirstAuthorization):
        disposition = (placement.AttemptCompleted(int(not outcome.run.passed)) if isinstance(outcome, CloudCompleted)
                       else placement.AttemptDeclined(outcome.reason))
        state = finish_assigned_run(authorization, attempt, disposition)
        if isinstance(state, placement.Pending):
            detail = f'{state.reason}. {outcome.reason}' if isinstance(outcome, CloudDeclined) else state.reason
            raise gate.AwsGateLaunchPending(detail)
        if isinstance(outcome, CloudDeclined) and isinstance(state, NoCloudLaunch):
            raise gate.AwsGateTookNoRun(outcome.reason)
    return outcome


def run_cloud(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
              service: Callable[[Sequence[str]], int], attempt: CloudAttempt,
              selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES) -> CloudDeclined | CloudCompleted:
    switch = aws_switch(settings())
    if not isinstance(switch, AwsOn):
        reason = cloud_gate_off_reason(switch)
        end_open_gates(attempt.directory, service)
        return CloudDeclined(reason)
    if isinstance(key_refusal.read(attempt.directory), key_refusal.KeyRefused):
        return CloudDeclined(key_refusal.REFUSED_REASON)
    directory = attempt.directory
    command_job = attempt.gate_job
    claim = attempt.claim
    assigned = isinstance(claim, placement.ProviderClaimReference)
    deployment_path = directory / 'gate-deployment.json'
    root = Path(gate.checked_git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'cloud-gates'))
    with control_lock(root):
        current = root / 'current.json'
        if not current.exists() and not deployment_path.exists():
            return CloudDeclined('image or stack is absent')
        tree, _dirty = working_tree(repo)
        require_selected_tree(repo, tree, selected)
        previous = read(current) if current.exists() else {}
        identity: dict[str, object] = {'tree': tree, 'commit': gate.checked_git(repo, 'rev-parse', 'HEAD'),
                                      'mode': mode.wire, 'full': full, 'module_time_limit_s': module_time_limit_s,
                                      'modules': list(selected.paths) if isinstance(selected, gate.TreeSelection) else 'all',
                                      'selection_tree': selected.tree if isinstance(selected, gate.TreeSelection) else 'all'}
        if previous.get('mode') == mode.value:
            previous['mode'] = mode.wire
        same_request = all(previous.get(key) == value for key, value in identity.items())
        book = ledger.CostLedger(directory)
        folder = root / ledger.text(previous, 'attempt') if same_request else root / uuid4().hex
        result_path = folder / 'outcome.json'
        saved_result = read(result_path) if result_path.exists() else {}
        ended = same_request and saved_result.get('status') != 'complete' and book.path.exists() and any(
            row.attempt == folder.name and not ledger.gate_is_open(row)
            for row in book.read_snapshot().gate_reservations)
        saved_request: dict[str, object] = {}
        if same_request and (not assigned or saved_result.get('status') == 'complete') and (folder / 'request.json').exists():
            try:
                saved_request = read_saved_request(repo, folder / 'request.json', tree, selected)
            except IncompatibleSavedGateRequest:
                if not assigned and not settle_incompatible_attempt(folder, book, service):
                    attempt.identifier = folder.name
                    attempt.gate_job = _saved_gate_job(previous)
                    return CloudDeclined('The incompatible cloud attempt has not ended')
                same_request = False
                ended = False
                folder = root / uuid4().hex
                result_path = folder / 'outcome.json'
                saved_result = {}
            except (OSError, ValueError, KeyError, TypeError):
                if not ended and saved_result.get('status') in (None, 'pending'):
                    raise
        if same_request and saved_result.get('status') == 'complete' and not _outcome_imported(folder):
            try:
                request = saved_request
                saved_attempt = request.setdefault('attempt', folder.name)
                if (request.get('tree') != identity['tree'] or request.get('head') != identity['commit']
                        or saved_attempt != folder.name):
                    raise ValueError('The saved request differs from the current cloud attempt')
                row = outcome_result(folder, saved_result)
            except (OSError, ValueError, KeyError, TypeError):
                pass
            else:
                attempt.identifier = folder.name
                attempt.gate_job = _saved_gate_job(previous)
                with route_reservation(repo, folder, mode, command_job, assigned=assigned) as reservation:
                    outcome = _complete_outcome(repo, folder, request, row)
                    reservation.status = int(isinstance(outcome, CloudCompleted) and not outcome.run.passed)
                    return outcome
        if not deployment_path.exists():
            return CloudDeclined('image or stack is absent')
        home = conf.read(account_settings_path()).text('region')
        _ = gate_regions(home)
        deployment = gate_deployment(deployment_path, home)
        regional = ledger.rows(deployment, 'regions')[0]
        if not regional.get('image') or not regional.get('template'):
            return CloudDeclined('image or stack is absent')
        first: ledger.GateReservation | None = None
        if assigned:
            rows = book.gate_claim_reservations(claim.claim_id)
            first = next((row for row in rows if row.attempt == claim.claim_id), None)
            if first is None or not isinstance(first.state, ledger.GateLaunchPending):
                return CloudDeclined('The first AWS booking is unavailable')
            folder = root / claim.claim_id
            same_request = False
            ended = False
            saved_request = {}
            saved_result = {}
            result_path = folder / 'outcome.json'
        retrying = same_request and saved_result.get('status') not in ('complete', 'fallback') and (
            bool(previous.get('retry_launch')) or retryable_cloud_failure(saved_result) is not RetryableCloudFailure.NONE)
        if not retrying and (ended or result_path.exists() and saved_result.get('status') != 'pending'):
            folder = root / uuid4().hex
            saved_request = {}
        elif same_request:
            attempt.gate_job = _saved_gate_job(previous)
        attempt.identifier = folder.name
        folder.mkdir(parents=True, exist_ok=True)
        retry_launch = ledger.mapping(previous.get('retry_launch', {})) if retrying else {}
        attempt.interruptions = int(str(previous.get('spot_interruptions', 0))) if retrying else 0
        counted_interruption = str(previous.get('counted_interruption', '')) if retrying else ''
        box_failures = int(str(previous.get('box_failures', 0))) if retrying else 0
        counted_box_failure = str(previous.get('counted_box_failure', '')) if retrying else ''
        last_box_failure = str(previous.get('last_box_failure', '')) if retrying else ''
        values = settings()
        retry_limit = values.whole('spot_interruption_retries')
        box_retry_limit = values.whole('box_failure_retries')
        if retrying:
            retry_limit = min(retry_limit, int(str(previous.get('spot_retry_limit', retry_limit))))
            box_retry_limit = min(box_retry_limit, int(str(previous.get('box_retry_limit', box_retry_limit))))
        with route_reservation(repo, folder, mode, command_job, assigned=assigned) as reservation:
            while True:
                state: dict[str, object] = {'attempt': folder.name, **identity,
                    'gate_job': snapshot_codec.snapshot_value(attempt.gate_job),
                    'spot_interruptions': attempt.interruptions, 'counted_interruption': counted_interruption,
                    'retry_launch': retry_launch, 'spot_retry_limit': retry_limit,
                    'box_failures': box_failures, 'counted_box_failure': counted_box_failure,
                    'box_retry_limit': box_retry_limit, 'last_box_failure': last_box_failure}
                save(current, state)
                result_path = folder / 'outcome.json'
                result = read(result_path) if result_path.exists() else {}
                failure = retryable_cloud_failure(result)
                if failure is not RetryableCloudFailure.NONE:
                    outcome = CloudDeclined(ledger.text(result, 'reason'))
                else:
                    if not (folder / 'launch.json').exists():
                        if attempt.interruptions > retry_limit:
                            reservation.status = 0
                            return CloudDeclined('The Spot interruption retry limit is reached')
                        if box_failures > box_retry_limit:
                            reservation.status = 0
                            return CloudDeclined(f'The box failure retry limit is reached: {last_box_failure}')
                        # A retry launches a new box, so it reads the switch again.
                        switch = aws_switch(settings())
                        if not isinstance(switch, AwsOn):
                            reservation.status = 0
                            return CloudDeclined(cloud_gate_off_reason(switch))
                    command = gate_attempt_command('--worker', folder, directory, reservation.sidecar)
                    request = saved_request or prepare(repo, folder, mode, module_time_limit_s,
                                                       selected=selected, gate_job=attempt.gate_job)
                    if request['tree'] != identity['tree'] or request['head'] != identity['commit']:
                        raise GateRefused('The checkout changed between cloud attempts')
                    if not accepts_suite_mode(repo, ledger.text(request, 'commit'), mode):
                        reservation.status = 0
                        return CloudDeclined(f'The tested commit does not read SuiteMode value {mode.wire!r}')
                    if _saved_gate_job(request) != attempt.gate_job:
                        request['gate_job'] = state['gate_job']
                    request['mode'] = mode.wire
                    request['full'] = full
                    if isinstance(command_job, StepJob) and command_job.step == 'test-modules':
                        request['named_modules'] = True
                        request['split'] = True
                        request['module_time_limit_s'] = values.whole('lifetime_minutes') * 60
                    if retry_launch:
                        request['retry_launch'] = retry_launch
                    if assigned:
                        request['placement_claim'] = claim.claim_id
                        if folder.name == claim.claim_id:
                            assert first is not None
                            request['expires'] = first.expires
                    save(folder / 'request.json', request)
                    save(folder / 'deployment.json', deployment)
                    if assigned and folder.name != claim.claim_id:
                        booked = reserve_assignment(book, values, claim, folder.name, ledger.instant(request, 'expires'))
                        if not isinstance(booked, ledger.GateReserved) or not booked.created:
                            return CloudDeclined(booked.reason if not isinstance(booked, ledger.GateReserved)
                                                 else 'The retry booking already exists')
                    attempt.booking_submitted = True
                    outcome = run_service(repo, folder, request, command, service)
                    result = read(result_path) if result_path.exists() else {}
                    failure = retryable_cloud_failure(result)
                if isinstance(outcome, CloudCompleted):
                    reservation.status = int(not outcome.run.passed)
                    return outcome
                reservation.status = 0
                if failure is RetryableCloudFailure.NONE:
                    return outcome
                if assigned and any(ledger.gate_is_open(row) for row in book.gate_claim_reservations(claim.claim_id)):
                    return outcome
                if failure is RetryableCloudFailure.SPOT:
                    if counted_interruption != folder.name:
                        attempt.interruptions += 1
                        counted_interruption = folder.name
                else:
                    last_box_failure = outcome.reason
                    if counted_box_failure != folder.name:
                        box_failures += 1
                        counted_box_failure = folder.name
                launch = read(folder / 'launch.json')
                next_variant = ledger.instant(launch, 'variant') + 1
                retry_launch = {key: launch[key] for key in ('variants', 'spot_price_reads') if key in launch}
                retry_launch['variant'] = next_variant
                save(current, {**state, 'spot_interruptions': attempt.interruptions,
                               'counted_interruption': counted_interruption, 'retry_launch': retry_launch,
                               'box_failures': box_failures, 'counted_box_failure': counted_box_failure,
                               'last_box_failure': last_box_failure})
                if attempt.interruptions > retry_limit:
                    return CloudDeclined('The Spot interruption retry limit is reached')
                if box_failures > box_retry_limit:
                    return CloudDeclined(f'The box failure retry limit is reached: {last_box_failure}')
                if next_variant >= len(ledger.rows(launch, 'variants')):
                    if failure is RetryableCloudFailure.BOX:
                        return CloudDeclined(f'Box failure: {last_box_failure}. No cloud capacity choice remains')
                    return CloudDeclined('No cloud capacity choice remains')
                folder = root / uuid4().hex
                saved_request = {}
                attempt.identifier = folder.name
                attempt.gate_job = command_job
                folder.mkdir()


def route_reservation(repo: Path, folder: Path, mode: gate.SuiteMode, job: StepJob | GateJobNotRecorded, *,
                      assigned: bool) -> AbstractContextManager[CloudReservation]:
    sidecar = folder / 'reservation.cgroup'
    if assigned:
        path = os.environ.get('BUILDLOG_PLACEMENT_SIDECAR')
        return nullcontext(CloudReservation(Path(path) if path else sidecar))
    return cloud_reservation(repo, sidecar, mode, gate_job=job)


def service_command(module: str, arguments: Sequence[str], unit: str, *, paired: bool = False,
                    start_only: bool = False) -> list[str]:
    """Use the installed scaler environment for each gate controller."""
    credential = str(admin_secrets.credentials_file())
    configuration = ['--setenv=CONF_DIR=' + os.environ['CONF_DIR']] if 'CONF_DIR' in os.environ else []
    owner = ['--setenv=' + name + '=' + value for name, value in admission_records.owner_environment().items()]
    cleanup: list[str] = []
    stop_timeout = settings().number('termination_timeout_s') + 2 * settings().number('api_timeout_s')
    if '--cleanup' not in arguments and '--reconcile' not in arguments:
        if paired:
            folder = arguments[arguments.index('--paired-worker') + 1]
            cleanup_arguments = ['--paired-cleanup', folder]
        else:
            cleanup_arguments = ['--cleanup' if value == '--worker' else value for value in arguments]
        command = shlex.join([sys.executable, '-m', module, *cleanup_arguments])
        cleanup = ['--property=ExecStopPost=' + command.replace('%', '%%').replace('$', '$$')]
    launch = ['--no-block'] if start_only else ['--wait', '--pipe']
    return ['systemd-run', '--user', *launch, '--collect', '--unit=' + unit,
            '--setenv=AWS_SHARED_CREDENTIALS_FILE=' + credential,
            *configuration, *owner, *cleanup,
            '--property=KillMode=' + ('mixed' if paired else 'control-group'), '--property=UMask=0077',
            '--property=TimeoutStopSec=' + str(stop_timeout),
            '--property=UnsetEnvironment=AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_CONFIG_FILE AWS_PROFILE',
            '--working-directory=' + str(Path.home() / '.claude'), sys.executable, '-m', module, *arguments]


def run_service(repo: Path, folder: Path, request: dict[str, object], command: Sequence[str],
                service: Callable[[Sequence[str]], int]) -> CloudDeclined | CloudCompleted:
    # The service owns every AWS call. The session only reads local evidence.
    _ = service(command)
    if not (folder / 'outcome.json').exists():
        return CloudDeclined('The scaler service has no final result')
    outcome = read(folder / 'outcome.json')
    if outcome.get('status') != 'complete':
        return CloudDeclined(ledger.text(outcome, 'reason'))
    try:
        row = outcome_result(folder, outcome)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return CloudDeclined(str(error))
    return _complete_outcome(repo, folder, request, row)


def _complete_outcome(repo: Path, folder: Path, request: dict[str, object],
                      row: dict[str, object]) -> CloudDeclined | CloudCompleted:
    try:
        validate_result(row, request)
        head = ledger.text(request, 'head')
        selection = gate_box.completion_selection(ledger.text(request, 'mode'), split=request.get('split') is True)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return CloudDeclined(str(error))
    if gate.checked_git(repo, 'rev-parse', 'HEAD') != head:
        return CloudDeclined('The checkout changed during the cloud attempt')
    record = {**ledger.mapping(row['record']), 'selection': selection}
    import_record(folder, {**row, 'record': record})
    print((folder / 'gate.log').read_text(), end='', flush=True)
    return CloudCompleted(gate_box.decode_run(row['gate']))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    _ = group.add_argument('--worker', type=Path)
    _ = group.add_argument('--cleanup', type=Path)
    _ = group.add_argument('--reconcile', action='store_true')
    _ = parser.add_argument('--directory', required=True, type=Path)
    _ = parser.add_argument('--sidecar', type=Path, help='required with --worker and --cleanup')
    _ = parser.add_argument('--settings', type=Path, default=account_settings_path())
    args = parser.parse_args()
    directory = cast(Path, args.directory)
    settings_path = cast(Path, args.settings)
    sidecar = cast(Path | None, args.sidecar)
    reconcile = cast(bool, args.reconcile)
    if sidecar is None and not reconcile:
        parser.error('--worker and --cleanup require --sidecar')

    def gate_worker() -> GateWorker:
        configuration = conf.read(settings_path)
        api = AwsCli(configuration.text('credentials_profile'), configuration.text('region'), settings().number('api_timeout_s'))
        # The reconcile step publishes no progress, so it has no sidecar.
        return GateWorker(api, ledger.CostLedger(directory), settings_path, sidecar or Path(os.devnull))

    if reconcile:
        gate_worker().reconcile()
        return 0
    cleanup = cast(Path | None, args.cleanup)
    folder = cleanup if cleanup is not None else cast(Path, args.worker)
    with control_lock(folder):
        worker = gate_worker()
        if cleanup is not None:
            with cancellation.defer_cleanup_signals():
                return int(not cleanup_folder(folder, worker))
        if (folder / 'outcome.json').exists() and read(folder / 'outcome.json').get('status') != 'pending':
            return 0
        outcome = worker.execute(folder, read(folder / 'request.json'), read(folder / 'deployment.json'))
        save(folder / 'outcome.json', outcome)
    return 0


@dataclass(frozen=True)
class NoCloudLaunch:
    """The booked claim ended without a launch."""


def reserve_assignment(book: ledger.CostLedger, values: conf.ConfigRead, claim: placement.ProviderClaimReference,
                       attempt: str, expires: int) -> ledger.GateReserved | ledger.GateCapReached | ledger.GateAttemptEnded:
    request = gate_booking(values, attempt, int(time.time()), expires)
    return book.reserve_gate(account_settings_path(), replace(request, placement_claim=claim.claim_id))


def finish_assigned_run(authorization: wire.FirstAuthorization, attempt: CloudAttempt,
                        outcome: placement.AttemptCompleted | placement.AttemptDeclined | placement.AttemptCancelled
                        ) -> NoCloudLaunch | placement.Settled | placement.Pending:
    """Report the outcome and keep the claim while any launch is open."""
    book = ledger.CostLedger(attempt.directory)
    claim_id = authorization.claim.claim_id
    state: NoCloudLaunch | placement.Settled | placement.Pending
    try:
        if not attempt.booking_submitted:
            book.resolve_gate(claim_id, ledger.GateAbsent(int(time.time())))
        rows = book.gate_claim_reservations(claim_id)
        if not rows or any(ledger.gate_is_open(row) for row in rows):
            state = placement.Pending('AWS launch settlement remains pending')
        elif all(isinstance(row.state, ledger.GateAbsent) for row in rows):
            state = NoCloudLaunch()
        else:
            state = placement.Settled()
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        state = placement.Pending('The AWS gate ledger is unavailable')
    if isinstance(state, NoCloudLaunch) and isinstance(outcome, placement.AttemptDeclined):
        # The caller submits the matching ClaimFailure.
        return state
    path = (placement.DispatchPathPending(state.reason) if isinstance(state, placement.Pending)
            else placement.DispatchPathSettled())
    evidence = execution_identity.ExecutionEvidenceUnavailable('AWS gate attempt ' + (attempt.identifier or 'did not start'),
                                                               time.time())
    try:
        _ = admission_client.report_execution(wire.ExecutionReport(admission_client.request_id(), authorization.assignment,
            authorization.claim, evidence, outcome, path))
    except (OSError, wire.AdmissionUnavailable) as error:
        print(f'AWS gate queue report failed: {error}', file=sys.stderr, flush=True)
    return state


def route(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
          assignment: placement.PlacementAssignment,
          selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES,
          gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED) -> gate.GateRun | str:
    """Book the exact claim before asking the queue to authorize a launch."""
    start = process_start(os.getpid())
    if not isinstance(start, Started):
        raise gate.AwsGateTookNoRun('The gate process has no live start time')
    holder = placement.ClaimHolderIdentity(store.host_name(), os.getpid(), start.start)
    claim = placement.assigned_claim_reference(assignment)
    book = ledger.CostLedger(target_config.ssh_target_state_directory('aws'))
    try:
        values = settings()
        switch = aws_switch(values)
        if not isinstance(switch, AwsOn):
            raise gate.AwsGateTookNoRun(cloud_gate_off_reason(switch))
        expires = int(time.time()) + values.whole('lifetime_minutes') * 60
        booking = reserve_assignment(book, values, claim, claim.claim_id, expires)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        raise gate.AwsGateLaunchPending(f'The AWS booking is unavailable: {error}') from error
    if isinstance(booking, ledger.GateCapReached):
        raise gate.AwsGateTookNoRun(booking.reason)
    if isinstance(booking, ledger.GateAttemptEnded):
        return booking.reason
    if not booking.created:
        raise gate.AwsGateLaunchPending('The AWS claim already has an open booking')
    authorized = False
    try:
        try:
            authorization = admission_client.consume_permission(
                wire.ConsumePermission(admission_client.request_id(), assignment, claim, holder))
        except (OSError, wire.AdmissionUnavailable) as error:
            raise gate.AwsGateTookNoRun(f'The AWS launch permission is unavailable: {error}') from error
        if not isinstance(authorization, wire.FirstAuthorization):
            raise gate.AwsGateTookNoRun(f'The queue refused AWS launch permission: {authorization.reason}')
        if authorization.assignment != assignment or authorization.claim != claim:
            raise gate.AwsGateTookNoRun('The AWS launch permission differs from the request')
        authorized = True
    finally:
        if not authorized:
            try:
                book.resolve_gate(claim.claim_id, ledger.GateAbsent(int(time.time())))
            except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
                raise gate.AwsGateLaunchPending(f'The AWS booking did not close: {error}') from error
    outcome = run(repo, mode=mode, full=full, module_time_limit_s=module_time_limit_s,
                  selected=selected, gate_job=gate_job, authorization=authorization)
    return outcome.reason if isinstance(outcome, CloudDeclined) else outcome.run


if __name__ == '__main__':
    raise SystemExit(main())

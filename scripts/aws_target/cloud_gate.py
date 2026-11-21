"""Run cloud gates through a scaler service and retain each attempt locally."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager
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
import time
from typing import BinaryIO, Literal, cast, final
from uuid import uuid4

from scripts.aws_target import admin_secrets, gate_box, ledger, run_cost, s3_presign
from scripts.aws_target.aws_cli import aws_error_code
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable, control_lock, fetch_account_charges
from scripts.aws_target.config import account_settings_path, gate_deployment, gate_regions, money, read_settings
from scripts.aws_target.run_cost import GateLaunchVariant as GateLaunchVariant
from scripts.build_queue import admission_client, admission_protocol as wire, cloud_progress, progress_profile, queue_place, snapshot_codec
from scripts.build_queue.admission_wire import CloudGroup, NoCloudGroup, NO_CLOUD_GROUP
from scripts.lib import cancellation
from scripts.build_targets import target_config
from scripts.buildlog import store
from scripts.lib import conf, git_index
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob
from scripts.lib.process_start import process_start, Started, Unreadable
from scripts.lib.atomic_file import write_text_atomic
from scripts.build_queue import admission_records
from scripts.production import affected_modules
from scripts.production.gate_modules import GateRefused, discover_test_modules
from scripts.production import merge_gate as gate
from scripts.test_times import gate_record


@dataclass(frozen=True)
class LocalFallback:
    reason: str


@dataclass
class CloudAttempt:
    directory: Path
    mode: gate.SuiteMode
    identifier: str = ''
    interruptions: int = 0
    gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED


@dataclass(frozen=True)
class CloudCompleted:
    run: gate.GateRun


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


class CloudGateSwitch(StrEnum):
    ON = 'on'
    OFF = 'off'


@dataclass(frozen=True)
class InvalidCloudGateSwitch:
    reason: str


type CloudGateSetting = CloudGateSwitch | InvalidCloudGateSwitch


def settings() -> conf.ConfigRead:
    return conf.read_or_repository('cloud_gate')


def launch_switch(configuration: conf.ConfigRead) -> CloudGateSetting:
    try:
        return configuration.value('enabled', CloudGateSwitch, recover_invalid=False)
    except conf.Unusable as error:
        return InvalidCloudGateSwitch(str(error))


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


def log_progress(text: str) -> progress_profile.ProgressCounter | progress_profile.NoCounter:
    """Read module completion lines from the current box log."""
    total = 0
    finished: set[str] = set()
    for line in text.splitlines():
        start = re.fullmatch(r'Runner: (\d+) modules, whole machine, \d+ CPUs', line)
        if start:
            total = int(start[1])
        result = re.fullmatch(r'(\S+) exit=-?\d+ seconds=\d+\.\d+(?: stopped)?', line)
        if result:
            finished.add(result[1])
    return (progress_profile.ProgressCounter('modules', min(len(finished), total), total)
            if total else progress_profile.NoCounter('The box has no module counter yet'))


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
    instance: str
    region: str


@dataclass(frozen=True)
class GateCleanupAttempt:
    reservation: ledger.GateReservation
    launch: dict[str, object]
    launch_record_checked: bool


@dataclass(frozen=True)
class UncertainGateLaunch:
    attempt: str
    regions: tuple[str, ...]
    filter: str
    expires: int


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
                attempts.append(GateCleanupAttempt(reservation, read(launch_path) if launch_path.exists() else {}, True))
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
        try:
            region = str(launch.get('region', reservation.region or worker.api.region))
            instance = str(launch.get('instance', ''))
            if not instance and isinstance(reservation.state, ledger.GateRunning):
                instance = reservation.state.instance_id
            if instance:
                boxes.append(GateTermination(reservation.attempt, instance, region))
            elif 'arguments' in launch:
                token = ledger.text(ledger.mapping(launch['arguments']), 'ClientToken')
                uncertain.append(UncertainGateLaunch(reservation.attempt, (region,),
                    'Name=client-token,Values=' + token, reservation.expires))
            elif item.launch_record_checked:
                worker.book.resolve_gate(reservation.attempt, ledger.GateAbsent(int(worker.clock())))
            else:
                regions = (reservation.region,) if reservation.region else tuple(settings().words('regions'))
                uncertain.append(UncertainGateLaunch(reservation.attempt, regions,
                    'Name=tag:GateAttempt,Values=' + reservation.attempt, reservation.expires))
        except (ValueError, KeyError, TypeError) as error:
            print('gate cleanup: ' + str(error), file=sys.stderr)
            complete = False
    boxes = worker.request_terminations(boxes, deadline)
    for launch in uncertain:
        try:
            found = False
            for region in launch.regions:
                remaining = deadline - worker.clock()
                if remaining <= 0:
                    raise AwsUnavailable('The gate cleanup deadline passed')
                reply = worker.regional_api(region).call('describe-instances', '--filters', launch.filter,
                    timeout_s=min(settings().number('api_timeout_s'), remaining))
                instances = [row for group in ledger.rows(reply, 'Reservations') for row in ledger.rows(group, 'Instances')]
                if instances:
                    discovered = [GateTermination(launch.attempt, ledger.text(row, 'InstanceId'), region) for row in instances]
                    try:
                        worker.book.gate_region(launch.attempt, region)
                    finally:
                        boxes.extend(worker.request_terminations(discovered, deadline))
                    found = True
                    break
            if not found:
                if worker.clock() < launch.expires:
                    complete = False
                else:
                    worker.book.resolve_gate(launch.attempt, ledger.GateAbsent(int(worker.clock())))
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


@dataclass(frozen=True)
class _ResultNotReceived:
    """No valid final result establishes an earlier box end time."""


_RESULT_NOT_RECEIVED = _ResultNotReceived()


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

    def download(self, bucket: str, key: str, path: Path, *, timeout_s: float | None = None) -> bool:
        try:
            _ = self.api.call('get-object', '--bucket', bucket, '--key', key, str(path), service='s3api', timeout_s=timeout_s)
            return True
        except AwsServiceFailure as error:
            if error.code in ('404', 'NoSuchKey'):
                return False
            raise
        except AwsUnavailable as error:
            if any(code in str(error) for code in ('NoSuchKey', '(404)', 'Not Found')):
                return False
            raise

    def terminate(self, attempt: str, instance: str, *,
                  result_seen_at: int | _ResultNotReceived = _RESULT_NOT_RECEIVED) -> None:
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
        ended_at = int(self.clock()) if isinstance(result_seen_at, _ResultNotReceived) else result_seen_at
        self.book.resolve_gate(attempt, ledger.GateTerminated(ended_at, instance))

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

    def record_ends(self, boxes: Sequence[GateTermination], region: str, ended: set[str]) -> list[GateTermination]:
        pending: list[GateTermination] = []
        for box in boxes:
            if box.region == region and box.instance in ended:
                try:
                    self.book.resolve_gate(box.attempt, ledger.GateTerminated(int(self.clock()), box.instance))
                    continue
                except (OSError, ValueError, KeyError, TypeError) as error:
                    print('paired cleanup: ' + str(error), file=sys.stderr)
            pending.append(box)
        return pending

    def request_terminations(self, boxes: Sequence[GateTermination], deadline: float) -> list[GateTermination]:
        """Request termination and return boxes that still need confirmation."""
        pending = list(boxes)
        for region in dict.fromkeys(box.region for box in boxes):
            instances = [box.instance for box in boxes if box.region == region]
            ended = self.confirmed_ends(region, 'terminate-instances', instances, deadline)
            if ended:
                pending = self.record_ends(pending, region, ended)
        return pending

    def confirm_terminations(self, boxes: Sequence[GateTermination], deadline: float) -> bool:
        """Record confirmed terminations under one shared deadline."""
        regions = tuple(dict.fromkeys(box.region for box in boxes))
        pending = list(boxes)
        while pending and self.clock() < deadline:
            for region in regions:
                instances = [box.instance for box in pending if box.region == region]
                if not instances or self.clock() >= deadline:
                    continue
                ended = self.confirmed_ends(region, 'describe-instances', instances, deadline)
                if ended:
                    pending = self.record_ends(pending, region, ended)
            if pending:
                self.pause(max(0, min(settings().number('result_poll_s'), deadline - self.clock())))
        return not pending

    def cleanup_due(self, reservation: ledger.GateReservation) -> bool:
        """Preserve live or unreadable workers before checking expiry."""
        if isinstance(reservation.owner, ledger.GateWorkerOwner):
            current = process_start(reservation.owner.pid)
            return not (isinstance(current, Unreadable)
                or isinstance(current, Started) and current.start == reservation.owner.start)
        return reservation.expires <= int(self.clock())

    def reconcile(self) -> None:
        """Clean expired or dead attempts while preserving live workers."""
        attempts: list[GateCleanupAttempt] = []
        for reservation in self.book.read_snapshot().gate_reservations:
            if isinstance(reservation.state, (ledger.GateAbsent, ledger.GateTerminated)) or not self.cleanup_due(reservation):
                continue
            launch: dict[str, object] = {}
            checked = False
            if isinstance(reservation.owner, ledger.GateWorkerOwner):
                folder = Path(reservation.owner.folder)
                launch_path = folder / 'launch.json'
                checked = (folder / 'request.json').exists()
                launch = read(launch_path) if launch_path.exists() else {}
            attempts.append(GateCleanupAttempt(reservation, launch, checked))
        _ = cleanup_attempts(attempts, self)

    def record_launch(self, request: dict[str, object], size: str, zone: str,
                      result: Literal['launched', 'refused', 'finished'], reason: str = '', *, gate_s: float = 0) -> None:
        error_code = launch_error_code(reason)
        now = self.clock()
        row: dict[str, object] = {'time': now, 'attempt': request['attempt'], 'instance_type': size,
            'vcpus': self.size_vcpus, 'result': result, 'mode': request['mode'], 'region': self.box_region}
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
        refused = [row for row in history.launch_rows
                   if row.get('result') == 'refused' and float(str(row['time'])) >= now - cooldown]
        zones = {(str(row.get('region', '')), str(row.get('instance_type', '')), str(row.get('availability_zone', '')))
                 for row in refused if row.get('error_code') in run_cost.ZONE_REFUSALS}
        cap = values.whole('max_gate_minutes')
        eligibility = run_cost.gate_variant_eligibility(variants, self.book.directory, history.evidence, mode,
            now=now, max_age=max_age, max_gate_minutes=cap, live_prices=live_prices)
        for (size, region), box in sorted(eligibility.slow_box_seconds.items()):
            print(f'Cloud gate skips {size} in {region}: its median gate time {run_cost.median_run_time([box])} '
                  + f'is above max_gate_minutes={cap}.', flush=True)
        try:
            quotas = {row.region: row for row in ledger.read_spot_quotas(self.book.directory) if 0 <= now - row.read_at <= max_age}
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
            quotas = {}
        try:
            snapshot = self.book.read_snapshot()
        except ledger.LedgerUnavailable:
            snapshot = ledger.LedgerEvidence()
        live = snapshot.live_gate_vcpus(now)
        marked = {row.pool for row in snapshot.gate_quota_refusals
                  if row.is_active(live.get(row.pool, 0), now, cooldown)}
        def capacity_order(variant: GateLaunchVariant) -> tuple[bool, bool, bool]:
            pool = ledger.SpotQuotaPool(variant.region, ledger.spot_quota_class(variant.instance_type))
            quota = quotas.get(pool.region)
            limit = quota.limit(pool.quota_class) if quota is not None else ledger.NoSpotQuota()
            key = (variant.region, variant.instance_type, variant.zone)
            full = isinstance(limit, Decimal) and live.get(pool, 0) + ledger.gate_vcpus(variant.instance_type) > limit
            return pool in marked, full, key in zones
        if not eligibility.prices:
            return sorted(eligibility.variants, key=capacity_order)
        pricing = run_cost.GateRunPricing.from_config(values, history.evidence, self.api.region, (mode,))
        def order(item: tuple[int, GateLaunchVariant]) -> tuple[tuple[bool, bool, bool], bool, Decimal, int, int]:
            position, variant = item
            price = eligibility.prices.get(variant)
            cost = pricing.expected_usd(price, variant.instance_type, variant.region) if price is not None else Decimal(0)
            return capacity_order(variant), price is None, cost, type_rank(variant), position
        return [variant for _, variant in sorted(enumerate(eligibility.variants), key=order)]

    def launch(self, folder: Path, request: dict[str, object], deployment: dict[str, object]) -> str | LocalFallback:
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
                return LocalFallback('The stack allows no configured gate type; run deploy.py up')
        variants: Sequence[GateLaunchVariant] = deployed.variants
        if not path.exists():
            if 'retry_launch' in request:
                launch = ledger.mapping(request['retry_launch'])
            else:
                readings = self.live_spot_prices(variants)
                live_prices = [price for reading in readings if reading.used for price in reading.prices]
                variants = self.rank_variants(variants, ledger.text(request, 'mode'), live_prices)
                if not variants:
                    self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateAbsent(int(self.clock())))
                    return LocalFallback('Every gate type and region has a median gate time above max_gate_minutes')
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
            if launch.get('arguments') and offset == index:
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
                          else gate.read_time_limit(vcpus, cast(list[str], request['modules']), vcpus,
                              request['mode'] == 'targeted-test' and not request.get('full', False)))
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
                save(path, launch)
            api = self.regional_api(self.box_region)
            self.book.gate_region(ledger.text(request, 'attempt'), self.box_region)
            self.size_vcpus = int(ledger.text(arguments, 'InstanceType').split('.')[1].removesuffix('xlarge')) * 4
            if self.clock() >= ledger.instant(request, 'expires'):
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
                return LocalFallback(f'{size} in {zone}: {launch_error_code(str(error))}')
            instances = ledger.rows(reply, 'Instances')
            if len(instances) != 1:
                raise AwsUnavailable('The launch reply has no unique instance')
            instance = ledger.text(instances[0], 'InstanceId')
            save(path, {**launch, 'instance': instance})
            self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateRunning(instance), instance_type=size)
            self.box_zone = zone
            return instance
        self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateAbsent(int(self.clock())))
        return LocalFallback('Spot capacity is unavailable in every configured region')

    def artifact_etags(self, bucket: str, prefix: str) -> dict[str, str]:
        listing = self.api.call('list-objects-v2', '--bucket', bucket, '--prefix', prefix,
                                '--no-paginate', service='s3api')
        return {ledger.text(row, 'Key'): ledger.text(row, 'ETag')
                for row in ledger.rows({'Contents': listing.get('Contents', [])}, 'Contents')}

    def execute(self, folder: Path, request: dict[str, object], deployment: dict[str, object]) -> dict[str, object]:
        attempt = ledger.text(request, 'attempt')
        row: dict[str, object] = {}
        interruption_code = ''
        interruption_confirmed = False
        result_seen_at: int | _ResultNotReceived = _RESULT_NOT_RECEIVED
        try:
            self.reconcile()
            previous = next((row for row in self.book.read_snapshot().gate_reservations if row.attempt == attempt), None)
            if previous is not None and isinstance(previous.state, (ledger.GateAbsent, ledger.GateTerminated)):
                return {'status': 'fallback', 'reason': 'The saved attempt has ended'}
            if not (folder / 'launch.json').exists():
                if request.get('task') != 'paired':
                    self.charges()
                reservation = self.book.reserve_gate(self.settings_path, ledger.GateReservation(
                    attempt, int(self.clock()), ledger.instant(request, 'expires'),
                    money(str(settings().number('spot_max_usd_per_hour'))),
                    money(str(settings().number('attempt_allowance_usd'))),
                    instance_type=settings().words('instance_types')[0]))
                if isinstance(reservation, (ledger.GateCapReached, ledger.GateAttemptEnded)):
                    return {'status': 'fallback', 'reason': reservation.reason}
                identity = process_start(os.getpid())
                if not isinstance(identity, Started):
                    raise ValueError('The worker has no readable process identity')
                self.book.gate_owner(attempt, ledger.GateWorkerOwner(os.getpid(), identity.start, str(folder.resolve())))
                bucket = ledger.text(deployment, 'bucket')
                if request['source_key']:
                    self.upload(bucket, ledger.text(request, 'source_key'), folder / 'source.bundle')
                self.upload(bucket, 'requests/' + attempt + '.json', folder / 'request.json')
                guest = folder / 'guest.py'
                _ = guest.write_text(gate_box.GUEST)
                self.upload(bucket, 'requests/' + attempt + '.guest.py', guest)
            identity = process_start(os.getpid())
            if not isinstance(identity, Started):
                raise ValueError('The worker has no readable process identity')
            self.book.gate_owner(attempt, ledger.GateWorkerOwner(os.getpid(), identity.start, str(folder.resolve())))
            launched = self.launch(folder, request, deployment)
            if isinstance(launched, LocalFallback):
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
                while self.clock() < ledger.instant(request, 'expires'):
                    now = time.monotonic()
                    deadline_reached = now >= deadline
                    stop_reason = ''
                    stop_code = ''
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
                        pass
                    except AwsUnavailable as error:
                        if not s3_outage.retry_after_failure(error, self.clock()):
                            raise
                    if ec2_error is not None:
                        raise ec2_error
                    if stop_reason:
                        if stop_code in SPOT_INTERRUPTION_CODES:
                            interruption_code = stop_code
                        raise ValueError(stop_reason)
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
                    self.terminate(attempt, instance, result_seen_at=result_seen_at)
                    interruption_confirmed = bool(interruption_code)
            return {'status': 'complete', 'instance': instance, 'result_file': RESULT_FILE}
        except (OSError, ValueError, KeyError, TypeError, AwsUnavailable, subprocess.SubprocessError) as error:
            launch = read(folder / 'launch.json') if (folder / 'launch.json').exists() else {}
            # Saved arguments mean AWS may have accepted the launch; a saved variant alone does not.
            if 'arguments' not in launch:
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
                                        ledger.text(request, 'mode'))
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


def working_tree(repo: Path) -> tuple[str, set[str]]:
    dirty = affected_modules.dirty_paths(repo)
    if not dirty:
        return gate.checked_git(repo, 'rev-parse', 'HEAD^{tree}'), dirty
    with tempfile.TemporaryDirectory(prefix='cloud-gate-index-') as temporary:
        index = Path(temporary) / 'index'
        git_index.private_copy(Path(gate.checked_git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'index')), index)
        environment = {**os.environ, 'GIT_INDEX_FILE': str(index)}
        _ = subprocess.run(['git', 'add', '-A'], cwd=repo, env=environment, check=True, capture_output=True)
        tree = subprocess.run(['git', 'write-tree'], cwd=repo, env=environment, check=True, capture_output=True, text=True)
        return tree.stdout.strip(), dirty


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


def prepare_request(repo: Path, folder: Path, mode: gate.SuiteMode, limit: float, *,
                    selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES) -> dict[str, object]:
    head = gate.checked_git(repo, 'rev-parse', 'HEAD')
    tree, dirty = working_tree(repo)
    if isinstance(selected, gate.TreeSelection) and tree != selected.tree:
        raise GateRefused('The checkout changed after the test selection')
    commit = gate.checked_git(repo, 'commit-tree', tree, '-p', head, '-m', 'Cloud gate snapshot') if dirty else head
    modules = discover_test_modules(repo)
    if isinstance(selected, gate.TreeSelection):
        paths = set(selected.paths)
        if not paths <= {str(path.relative_to(repo)) for _, path in modules}:
            raise GateRefused('selected modules are absent from the tested tree')
        modules = [(suite, path) for suite, path in modules if str(path.relative_to(repo)) in paths]
    origin_main = gate.checked_git(repo, 'rev-parse', 'refs/remotes/origin/main')
    origin = gate.checked_git(repo, 'remote', 'get-url', 'origin')
    from urllib.parse import urlsplit
    if urlsplit(origin).password or urlsplit(origin).query or ('://' in origin and urlsplit(origin).username):
        raise GateRefused('The origin URL must contain no credential')
    folder.mkdir(parents=True, exist_ok=True)
    request: dict[str, object] = {'attempt': folder.name, 'commit': commit, 'head': head, 'tree': tree, 'dirty_paths': sorted(dirty), 'origin': origin,
        'origin_main': origin_main, 'clone_url': settings().text('clone_url'),
        'mode': mode.value, 'modules': [str(p.relative_to(repo)) for _, p in modules], 'source_key': '',
        'expires': int(time.time()) + settings().whole('lifetime_minutes') * 60,
        'module_time_limit_s': min(limit, settings().number('module_time_limit_s'))}
    return request


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


def run(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
        selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES,
        gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED,
        service: Callable[[Sequence[str]], int] = lambda command: subprocess.call(command)) -> LocalFallback | CloudCompleted:
    # Record the final fallback here so worker replies and service errors produce one row.
    attempt = CloudAttempt(target_config.ssh_target_state_directory('aws'), mode, gate_job=gate_job)
    try:
        outcome = run_cloud(repo, mode=mode, full=full, module_time_limit_s=module_time_limit_s,
                            selected=selected, service=service, attempt=attempt)
    except (OSError, ValueError, KeyError, TypeError, AwsUnavailable, subprocess.SubprocessError,
            GateRefused, affected_modules.GraphError) as error:
        outcome = LocalFallback(str(error))
    if isinstance(outcome, LocalFallback):
        if attempt.interruptions:
            outcome = LocalFallback(f'Spot interruption on {attempt.interruptions} attempts; ' + outcome.reason)
        try:
            append_launch(attempt.directory, {'time': time.time(), 'attempt': attempt.identifier,
                'instance_type': '', 'vcpus': 0, 'result': 'fallback', 'mode': attempt.mode.value, 'region': '',
                'availability_zone': '', 'error_code': launch_error_code(outcome.reason), 'reason': outcome.reason},
                gate_job=attempt.gate_job)
        except OSError as error:
            detail = ' '.join(str(error).split())
            print('Cloud gate fallback row was not written: ' + detail, file=sys.stderr, flush=True)
    return outcome


def run_cloud(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
              service: Callable[[Sequence[str]], int], attempt: CloudAttempt,
              selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES) -> LocalFallback | CloudCompleted:
    switch = launch_switch(settings())
    if isinstance(switch, InvalidCloudGateSwitch):
        return LocalFallback(switch.reason)
    if switch is CloudGateSwitch.OFF:
        return LocalFallback('cloud gates are off (cloud_gate.conf enabled=off)')
    directory = attempt.directory
    command_job = attempt.gate_job
    deployment_path = directory / 'gate-deployment.json'
    root = Path(gate.checked_git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'cloud-gates'))
    with control_lock(root):
        current = root / 'current.json'
        if not current.exists() and not deployment_path.exists():
            return LocalFallback('image or stack is absent')
        tree, _dirty = working_tree(repo)
        previous = read(current) if current.exists() else {}
        identity: dict[str, object] = {'tree': tree, 'commit': gate.checked_git(repo, 'rev-parse', 'HEAD'),
                                      'mode': mode.value, 'full': full, 'module_time_limit_s': module_time_limit_s,
                                      'modules': list(selected.paths) if isinstance(selected, gate.TreeSelection) else 'all',
                                      'selection_tree': selected.tree if isinstance(selected, gate.TreeSelection) else 'all'}
        same_request = all(previous.get(key) == value for key, value in identity.items())
        folder = root / ledger.text(previous, 'attempt') if same_request else root / uuid4().hex
        result_path = folder / 'outcome.json'
        saved_result = read(result_path) if result_path.exists() else {}
        if same_request and saved_result.get('status') == 'complete' and not _outcome_imported(folder):
            try:
                request = read(folder / 'request.json')
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
                with cloud_reservation(repo, folder / 'reservation.cgroup', mode, gate_job=command_job) as reservation:
                    outcome = _complete_outcome(repo, folder, request, row)
                    reservation.status = int(isinstance(outcome, CloudCompleted) and not outcome.run.passed)
                    return outcome
        if not deployment_path.exists():
            return LocalFallback('image or stack is absent')
        home = conf.read(account_settings_path()).text('region')
        _ = gate_regions(home)
        deployment = gate_deployment(deployment_path, home)
        regional = ledger.rows(deployment, 'regions')[0]
        if not regional.get('image') or not regional.get('template'):
            return LocalFallback('image or stack is absent')
        retrying = same_request and saved_result.get('status') not in ('complete', 'fallback') and (
            bool(previous.get('retry_launch')) or retryable_cloud_failure(saved_result) is not RetryableCloudFailure.NONE)
        book = ledger.CostLedger(directory)
        ended = book.path.exists() and any(
            row.attempt == folder.name and isinstance(row.state, (ledger.GateAbsent, ledger.GateTerminated))
            for row in book.read_snapshot().gate_reservations)
        if not retrying and (ended or result_path.exists() and saved_result.get('status') != 'pending'):
            folder = root / uuid4().hex
        elif same_request:
            attempt.gate_job = _saved_gate_job(previous)
        attempt.identifier = folder.name
        folder.mkdir(parents=True, exist_ok=True)
        sidecar = folder / 'reservation.cgroup'
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
        with cloud_reservation(repo, sidecar, mode, gate_job=command_job) as reservation:
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
                    outcome = LocalFallback(ledger.text(result, 'reason'))
                else:
                    if not (folder / 'launch.json').exists():
                        if attempt.interruptions > retry_limit:
                            reservation.status = 0
                            return LocalFallback('The Spot interruption retry limit is reached')
                        if box_failures > box_retry_limit:
                            reservation.status = 0
                            return LocalFallback(f'The box failure retry limit is reached: {last_box_failure}')
                    command = service_command('scripts.aws_target.cloud_gate',
                        ['--worker', str(folder), '--directory', str(directory), '--sidecar', str(sidecar),
                         '--settings', str(account_settings_path())], 'cloud-gate-' + folder.name)
                    request = (read(folder / 'request.json') if (folder / 'request.json').exists()
                               else prepare(repo, folder, mode, module_time_limit_s,
                                            selected=selected, gate_job=attempt.gate_job))
                    if request['tree'] != identity['tree'] or request['head'] != identity['commit']:
                        raise GateRefused('The checkout changed between cloud attempts')
                    if _saved_gate_job(request) != attempt.gate_job:
                        request['gate_job'] = state['gate_job']
                    request['full'] = full
                    if retry_launch:
                        request['retry_launch'] = retry_launch
                    save(folder / 'request.json', request)
                    save(folder / 'deployment.json', deployment)
                    outcome = run_service(repo, folder, request, command, service)
                    result = read(result_path) if result_path.exists() else {}
                    failure = retryable_cloud_failure(result)
                if isinstance(outcome, CloudCompleted):
                    reservation.status = int(not outcome.run.passed)
                    return outcome
                reservation.status = 0
                if failure is RetryableCloudFailure.NONE:
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
                    return LocalFallback('The Spot interruption retry limit is reached')
                if box_failures > box_retry_limit:
                    return LocalFallback(f'The box failure retry limit is reached: {last_box_failure}')
                if next_variant >= len(ledger.rows(launch, 'variants')):
                    if failure is RetryableCloudFailure.BOX:
                        return LocalFallback(f'Box failure: {last_box_failure}. No cloud capacity choice remains')
                    return LocalFallback('No cloud capacity choice remains')
                folder = root / uuid4().hex
                attempt.identifier = folder.name
                attempt.gate_job = command_job
                folder.mkdir()


def service_command(module: str, arguments: Sequence[str], unit: str, *, paired: bool = False) -> list[str]:
    """Use the installed scaler environment for each gate controller."""
    credential = str(admin_secrets.credentials_file())
    configuration = ['--setenv=CONF_DIR=' + os.environ['CONF_DIR']] if 'CONF_DIR' in os.environ else []
    owner = ['--setenv=' + name + '=' + os.environ[name] for name in
             ('CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_SESSION_NAME', admission_records.BUILDLOG_UNIT)
             if name in os.environ]
    cleanup: list[str] = []
    stop_timeout = settings().number('termination_timeout_s') + 2 * settings().number('api_timeout_s')
    if paired:
        folder = arguments[arguments.index('--paired-worker') + 1]
        command = shlex.join([sys.executable, '-m', module, '--paired-cleanup', folder])
    else:
        cleanup_arguments = ['--cleanup' if value == '--worker' else value for value in arguments]
        command = shlex.join([sys.executable, '-m', module, *cleanup_arguments])
    cleanup = ['--property=ExecStopPost=' + command.replace('%', '%%').replace('$', '$$')]
    return ['systemd-run', '--user', '--wait', '--pipe', '--collect', '--unit=' + unit,
            '--setenv=AWS_SHARED_CREDENTIALS_FILE=' + credential,
            *configuration, *owner, *cleanup,
            '--property=KillMode=' + ('mixed' if paired else 'control-group'), '--property=UMask=0077',
            '--property=TimeoutStopSec=' + str(stop_timeout),
            '--property=UnsetEnvironment=AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_CONFIG_FILE AWS_PROFILE',
            '--working-directory=' + str(Path.home() / '.claude'), sys.executable, '-m', module, *arguments]


def run_service(repo: Path, folder: Path, request: dict[str, object], command: Sequence[str],
                service: Callable[[Sequence[str]], int]) -> LocalFallback | CloudCompleted:
    # The service owns every AWS call. The session only reads local evidence.
    _ = service(command)
    if not (folder / 'outcome.json').exists():
        return LocalFallback('The scaler service has no final result')
    outcome = read(folder / 'outcome.json')
    if outcome.get('status') != 'complete':
        return LocalFallback(ledger.text(outcome, 'reason'))
    try:
        row = outcome_result(folder, outcome)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return LocalFallback(str(error))
    return _complete_outcome(repo, folder, request, row)


def _complete_outcome(repo: Path, folder: Path, request: dict[str, object],
                      row: dict[str, object]) -> LocalFallback | CloudCompleted:
    try:
        validate_result(row, request)
        head = ledger.text(request, 'head')
    except (OSError, ValueError, KeyError, TypeError) as error:
        return LocalFallback(str(error))
    if gate.checked_git(repo, 'rev-parse', 'HEAD') != head:
        return LocalFallback('The checkout changed during the cloud attempt')
    import_record(folder, row)
    print((folder / 'gate.log').read_text(), end='', flush=True)
    return CloudCompleted(gate_box.decode_run(row['gate']))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    _ = group.add_argument('--worker', type=Path)
    _ = group.add_argument('--cleanup', type=Path)
    _ = parser.add_argument('--directory', required=True, type=Path)
    _ = parser.add_argument('--sidecar', required=True, type=Path)
    _ = parser.add_argument('--settings', type=Path, default=account_settings_path())
    args = parser.parse_args()
    cleanup = cast(Path | None, args.cleanup)
    folder = cleanup if cleanup is not None else cast(Path, args.worker)
    directory = cast(Path, args.directory)
    with control_lock(folder):
        settings_path = cast(Path, args.settings)
        configuration = conf.read(settings_path)
        api = AwsCli(configuration.text('credentials_profile'), configuration.text('region'), settings().number('api_timeout_s'))
        worker = GateWorker(api, ledger.CostLedger(directory), settings_path, cast(Path, args.sidecar))
        if cleanup is not None:
            with cancellation.defer_cleanup_signals():
                return int(not cleanup_folder(folder, worker))
        if (folder / 'outcome.json').exists() and read(folder / 'outcome.json').get('status') != 'pending':
            return 0
        outcome = worker.execute(folder, read(folder / 'request.json'), read(folder / 'deployment.json'))
        save(folder / 'outcome.json', outcome)
    return 0


def route(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
          selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES,
          gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED) -> gate.GateRun | str:
    """Return a completed gate or the reason for local fallback."""
    outcome = run(repo, mode=mode, full=full, module_time_limit_s=module_time_limit_s,
                  selected=selected, gate_job=gate_job)
    return outcome.reason if isinstance(outcome, LocalFallback) else outcome.run


if __name__ == '__main__':
    raise SystemExit(main())

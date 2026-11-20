"""Compare local revisions on disposable gate instances."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, redirect_stdout, suppress
from dataclasses import asdict, dataclass
from decimal import Decimal
from datetime import UTC, datetime
from enum import Enum
import json
import multiprocessing
import os
import re
import sys
from multiprocessing.connection import wait
from multiprocessing.process import BaseProcess
from pathlib import Path
import signal
import socket
import statistics
import subprocess
import time
from typing import Protocol, cast
from uuid import uuid4

from scripts.aws_target.gate_box import (
    GUEST as GUEST, user_data as user_data,
    boot_trap as boot_trap, log_push_loop as log_push_loop,
    RUN_FIELDS as RUN_FIELDS, validate_paired_result as validate_paired_result,
)
from scripts.aws_target import ledger, cloud_gate
from scripts.aws_target.s3_presign import SigningKey as SigningKey, presign as presign
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable
from scripts.aws_target.config import read_settings, account_settings_path, gate_deployment, gate_regions
from scripts.lib import conf, file_lock
from scripts.build_queue import snapshot_codec
from scripts.build_queue.admission_jobs import StepJob
from scripts.lib.process_group import send_process_group
from scripts.lib import cancellation
from scripts.build_queue.admission_records import process_start_time
from scripts.build_queue.admission_wire import CloudGroup, Owner
from scripts.build_targets import target_config
from scripts.production.gate_modules import GateRefused


NIXOS_LOCK = Path('/etc/nixos/flake.lock')


def host_nixpkgs_revision() -> str:
    """Read the host's locked nixpkgs input before creating trial resources."""
    lock = ledger.mapping(cast(object, json.loads(NIXOS_LOCK.read_text())))
    nodes = ledger.mapping(lock['nodes'])
    inputs = ledger.mapping(ledger.mapping(nodes[ledger.text(lock, 'root')])['inputs'])
    locked = ledger.mapping(ledger.mapping(nodes[ledger.text(inputs, 'nixpkgs')])['locked'])
    return ledger.text(locked, 'rev')


TRIAL_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
PAIRED_TRIAL_JOB = StepJob('test-modules', modules=1)
SERVICE_TIMEOUT_FILE = 'service-timeout.json'


def main(argv: Sequence[str], settings_path: Path, directory: Path) -> int:
    if '--paired-cleanup' in argv:
        return paired_cleanup_main(argv, settings_path, directory)
    if '--paired-worker' in argv:
        return paired_worker_main(argv, settings_path, directory)
    return paired_main(argv, directory)


@dataclass(frozen=True)
class TrialRevision:
    commit: str
    tree: str


@dataclass(frozen=True)
class PairedTrialRequest:
    base: TrialRevision
    changed: TrialRevision
    modules: tuple[str, ...]
    rounds: int
    clone_url: str


class TrialWait(Enum):
    ADMISSION = 'cloud slot admission'
    LAUNCH = 'the launch decision'
    RESERVATION_LOCK = 'the paired reservation lock'
    LEDGER_LOCK = 'the cost ledger lock'
    COST_REFRESH = 'the trial cost refresh'
    WORKER_EXIT = 'paired workers to finish'
    WORKER_REAP = 'paired workers to exit after a stop'
    LOCAL_GIT = 'local Git'
    NETWORK_GIT = 'network Git'
    SERVICE = 'the paired service to finish'
    SERVICE_RUNTIME = 'the paired service within its systemd time limit'
    SERVICE_STOP = 'systemd to stop the paired service'
    COMMAND_REAP = 'a stopped trial command to exit'


class TrialWaitExpired(GateRefused):
    def __init__(self, wait_for: TrialWait) -> None:
        self.wait_for: TrialWait = wait_for
        super().__init__(f'Timed out waiting for {wait_for.value}')


class TrialServiceStopUnconfirmed(GateRefused):
    def __init__(self, status: int) -> None:
        super().__init__(f'systemd did not confirm the paired service stop. systemctl returned {status}')


@dataclass(frozen=True)
class TrialDeadline:
    expires_at: float

    @classmethod
    def after(cls, seconds: float) -> TrialDeadline:
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float:
        return max(0, self.expires_at - time.monotonic())

    def check(self, wait_for: TrialWait) -> None:
        if not self.remaining():
            raise TrialWaitExpired(wait_for)


def cleanup_seconds(settings: conf.ConfigRead) -> float:
    return settings.number('termination_timeout_s') + 2 * settings.number('api_timeout_s')


def preparation_seconds(settings: conf.ConfigRead, modules: int) -> float:
    # Allow reconciliation, reservation and billing locks, and one lock for each module.
    return (cleanup_seconds(settings) + settings.number('paired_cost_refresh_timeout_s')
            + settings.number('api_timeout_s') * (modules + 3))


@contextmanager
def trial_lock(path: Path, deadline: TrialDeadline, wait_for: TrialWait) -> Generator[file_lock.LockResult]:
    retry_seconds = cloud_gate.settings().number('paired_lock_retry_s')
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        deadline.check(wait_for)
        with file_lock.exclusive(path, wait=False) as result:
            if result is file_lock.LockResult.ACQUIRED:
                yield result
                return
        time.sleep(min(retry_seconds, deadline.remaining()))


def trial_ledger(directory: Path) -> ledger.CostLedger:
    timeout = cloud_gate.settings().number('api_timeout_s')
    return ledger.CostLedger(directory, mutation_lock=lambda path:
        trial_lock(path, TrialDeadline.after(timeout), TrialWait.LEDGER_LOCK))


def trial_command(command: Sequence[str], wait_for: TrialWait, *, timeout: float,
                  capture: bool = False) -> subprocess.CompletedProcess[str]:
    reap_timeout = cloud_gate.settings().number('api_timeout_s')
    pipe = subprocess.PIPE if capture else None
    process = subprocess.Popen(command, stdout=pipe, stderr=pipe, text=True, start_new_session=True)
    try:
        try:
            if capture:
                output, errors = process.communicate(timeout=timeout)
            else:
                _ = process.wait(timeout=timeout)
                output = errors = ''
        except subprocess.TimeoutExpired as error:
            raise TrialWaitExpired(wait_for) from error
        assert process.returncode is not None
        return subprocess.CompletedProcess(command, process.returncode, output, errors)
    finally:
        try:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    send_process_group(process.pid, signal.SIGKILL)
                try:
                    _ = process.wait(timeout=reap_timeout)
                except subprocess.TimeoutExpired as error:
                    raise TrialWaitExpired(TrialWait.COMMAND_REAP) from error
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def checked_git(repo: Path, *arguments: str) -> str:
    command = ['git', '-C', str(repo), *arguments]
    wait_for = TrialWait.NETWORK_GIT if arguments and arguments[0] in ('push', 'ls-remote') else TrialWait.LOCAL_GIT
    result = trial_command(command, wait_for, timeout=cloud_gate.settings().number('git_network_timeout_s'), capture=True)
    result.check_returncode()
    return result.stdout.strip()


def trial_request(repo: Path, base: str, changed: str, modules: Sequence[str], rounds: int) -> PairedTrialRequest:
    """Resolve local commits without changing the source checkout."""
    from urllib.parse import urlsplit
    if rounds <= 0 or not modules or len(set(modules)) != len(modules):
        raise ValueError('Use positive rounds and distinct modules')
    for module in modules:
        path = Path(module)
        if path.is_absolute() or '..' in path.parts or len(path.parts) != 2 or not path.name.endswith('.py'):
            raise ValueError('Use modules as <folder>/<file.py>')
    url = cloud_gate.settings().text('clone_url')
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query:
        raise ValueError('The clone URL must contain no credential')
    revisions: list[TrialRevision] = []
    for ref in (base, changed):
        commit = checked_git(repo, 'rev-parse', '--verify', ref + '^{commit}')
        tree = checked_git(repo, 'rev-parse', commit + '^{tree}')
        for module in modules:
            if checked_git(repo, 'cat-file', '-t', commit + ':scripts/' + module) != 'blob':
                raise ValueError('The module is absent from a requested tree')
        revisions.append(TrialRevision(commit, tree))
    return PairedTrialRequest(revisions[0], revisions[1], tuple(modules), rounds, url)


def delete_trial_branches(repo: Path, clone_url: str, prefix: str, expires: int, trial_id: str) -> None:
    """Find and delete remote branches in this trial's unique namespace."""
    namespace = f'refs/heads/{prefix}-{expires}-{trial_id}-'
    try:
        remote = checked_git(repo, 'ls-remote', '--heads', clone_url, namespace + '*')
    except TrialWaitExpired as error:
        print(f'paired trial: could not list branches for trial {trial_id}. {error}', file=sys.stderr)
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        print(f'paired trial: could not list branches for trial {trial_id}', file=sys.stderr)
        return
    timeouts: list[TrialWaitExpired] = []
    for line in remote.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[1].startswith(namespace):
            continue
        branch = fields[1]
        try:
            _ = checked_git(repo, 'push', clone_url, '--delete', branch)
        except TrialWaitExpired as error:
            print(f'paired trial: could not delete branch {branch.removeprefix("refs/heads/")}. {error}', file=sys.stderr)
            timeouts.append(error)
        except (OSError, ValueError, subprocess.SubprocessError):
            print(f'paired trial: could not delete branch {branch.removeprefix("refs/heads/")}', file=sys.stderr)
    if timeouts:
        raise timeouts[0]


def sweep_trial_branches(repo: Path, clone_url: str, prefix: str, now: int) -> None:
    """Delete expired trial branches without blocking a new trial on failure."""
    namespace = f'refs/heads/{prefix}-'
    try:
        remote = checked_git(repo, 'ls-remote', '--heads', clone_url, namespace + '*')
        for line in remote.splitlines():
            fields = line.split()
            if len(fields) != 2 or not fields[1].startswith(namespace):
                continue
            name = fields[1].removeprefix(namespace)
            match = re.fullmatch(r'([0-9]{1,12})-([0-9a-f]{32})-(base|changed)', name)
            if match and int(match[1]) < now:
                _ = checked_git(repo, 'push', clone_url, '--delete', fields[1])
    except (OSError, ValueError, subprocess.SubprocessError):
        print(f'paired trial: could not sweep old branches for {prefix}', file=sys.stderr)


def module_request(trial: PairedTrialRequest, module: str, attempt: str, expires: int) -> dict[str, object]:
    return {'task': 'paired', 'attempt': attempt, 'base': asdict(trial.base), 'changed': asdict(trial.changed),
            'commit': trial.changed.commit, 'tree': trial.changed.tree, 'head': trial.changed.commit,
            'origin': trial.clone_url, 'clone_url': trial.clone_url, 'mode': 'targeted-test',
            'module': module, 'modules': ['scripts/' + module], 'rounds': trial.rounds, 'source_key': '',
            'expires': expires, 'gate_job': snapshot_codec.snapshot_value(PAIRED_TRIAL_JOB),
            'module_time_limit_s': cloud_gate.settings().number('module_time_limit_s'),
            'delivery_s': cloud_gate.settings().number('termination_timeout_s') + 2 * cloud_gate.settings().number('api_timeout_s')}


def module_summary(module: str, rounds: int, runs: Sequence[dict[str, object]]) -> dict[str, object]:
    def median(variant: str, field: str) -> float | None:
        values = [float(str(row[field])) for row in runs if row['variant'] == variant and row.get(field) is not None]
        return statistics.median(values) if values else None
    pairs = {(str(row['variant']), int(str(row['round']))): row for row in runs}
    savings: list[float] = []
    for number in range(1, rounds + 1):
        base, changed = pairs.get(('base', number), {}), pairs.get(('changed', number), {})
        if base.get('cpu_s') is not None and changed.get('cpu_s') is not None:
            savings.append(float(str(base['cpu_s'])) - float(str(changed['cpu_s'])))
    complete = len(pairs) == 2 * rounds
    counts = {row.get('tests') for row in runs}
    return {'module': module, 'rounds': rounds, 'base_median_cpu_s': median('base', 'cpu_s'),
            'changed_median_cpu_s': median('changed', 'cpu_s'),
            'paired_median_saving_cpu_s': statistics.median(savings) if savings else None,
            'base_median_peak_mib': median('base', 'peak_mib'), 'changed_median_peak_mib': median('changed', 'peak_mib'),
            'same_test_count': complete and len(counts) == 1 and None not in counts,
            'all_passed': complete and all(row['status'] == 0 and row.get('tests') is not None
                                          and row.get('failures') == 0 and row.get('errors') == 0 for row in runs)}


def reserve_trial(worker: cloud_gate.GateWorker, requests: Sequence[dict[str, object]],
                  record_query: Callable[[Decimal], None] = lambda _fee: None, *,
                  check_workers: Callable[[], None] = lambda: None,
                  before_charge_page: Callable[[], None] = lambda: None) -> None:
    """Reserve every box before the coordinator permits any launch."""
    check_workers()
    worker.reconcile()
    check_workers()
    refresh_deadline = TrialDeadline.after(cloud_gate.settings().number('paired_cost_refresh_timeout_s'))
    page_deadline = refresh_deadline

    def page_timeout(api_timeout: float) -> float:
        nonlocal page_deadline
        before_charge_page()
        page_deadline = TrialDeadline(min(refresh_deadline.expires_at, time.monotonic() + api_timeout))
        remaining = page_deadline.remaining()
        if not remaining:
            raise TrialWaitExpired(TrialWait.COST_REFRESH)
        return remaining

    def after_page() -> None:
        page_deadline.check(TrialWait.COST_REFRESH)
        before_charge_page()

    try:
        worker.charges(record_query, page_timeout=page_timeout, after_page=after_page)
    except subprocess.TimeoutExpired as error:
        raise TrialWaitExpired(TrialWait.COST_REFRESH) from error
    check_workers()
    fallback = Decimal(str(cloud_gate.settings().number('spot_max_usd_per_hour')))
    readings = worker.book.read_bills()
    day = datetime.fromtimestamp(worker.clock(), UTC).date().isoformat()
    rate, reserved_type = max((ledger.gate_rate(readings, size, fallback, int(worker.clock()), day).usd_per_hour, size)
                              for size in cloud_gate.settings().words('instance_types'))
    policy = read_settings(worker.settings_path)
    state = worker.book.read_snapshot()
    now = int(worker.clock())
    costs = ledger.costs_from_evidence(state, policy, now)
    daily_future, monthly_future = ledger.gate_future_costs(state, policy, now)
    daily_room = policy.daily_limit_usd - costs.daily.usd - daily_future - policy.limit_reserve_usd
    monthly_room = policy.monthly_limit_usd - costs.monthly.usd - monthly_future - policy.limit_reserve_usd
    estimate = sum((ledger.box_cost(ledger.BoxRate(rate, 'trial reserve'), ledger.instant(row, 'expires') - now,
                   policy.box_minimum_seconds) + Decimal(str(cloud_gate.settings().number('attempt_allowance_usd')))
                    for row in requests), Decimal(0))
    print(f'Paired trial estimate: ${estimate}; daily room: ${daily_room}; monthly room: ${monthly_room}', file=sys.stderr)
    reserved: list[str] = []
    try:
        for request in requests:
            check_workers()
            attempt = ledger.text(request, 'attempt')
            # Use the highest rate until AWS confirms the actual type.
            reply = worker.book.reserve_gate(worker.settings_path, ledger.GateReservation(
                attempt, int(worker.clock()), ledger.instant(request, 'expires'), fallback,
                Decimal(str(cloud_gate.settings().number('attempt_allowance_usd'))), instance_type=reserved_type))
            if isinstance(reply, (ledger.GateCapReached, ledger.GateAttemptEnded)):
                raise GateRefused(reply.reason)
            reserved.append(attempt)
        check_workers()
    except BaseException:
        for attempt in reserved:
            worker.book.resolve_gate(attempt, ledger.GateAbsent(int(worker.clock())))
        raise


def trial_cost(book: ledger.CostLedger, attempts: set[str], settings_path: Path, now: int) -> tuple[Decimal, bool]:
    """Price only this trial; retain uncertain launch costs."""
    policy = read_settings(settings_path)
    total = Decimal(0)
    complete = True
    state = book.priced_snapshot(now)
    intervals = ledger.gate_compute_intervals(state, now, policy.box_minimum_seconds, reserve=True)
    for reservation in state.gate_reservations:
        if reservation.attempt not in attempts or isinstance(reservation.state, ledger.GateAbsent):
            continue
        confirmed = isinstance(reservation.state, ledger.GateTerminated)
        complete = complete and confirmed
        total += sum((row.usd for row in intervals if row.source == 'gate:' + reservation.attempt), Decimal(0))
        total += ledger.gate_allowance(reservation).usd
    return total, complete


def cleanup_trial(folder: Path, worker: cloud_gate.GateWorker) -> bool:
    return cloud_gate.cleanup_folder(folder, worker)


def sweep_trial_resources(folder: Path, worker: cloud_gate.GateWorker) -> None:
    """Clean expired trials before a new trial reserves boxes."""
    unresolved = {row.attempt for row in worker.book.read_snapshot().gate_reservations
                  if not isinstance(row.state, (ledger.GateAbsent, ledger.GateTerminated)) and worker.cleanup_due(row)}
    for previous in sorted(folder.parent.iterdir()):
        if previous == folder or not previous.is_dir():
            continue
        requests = [(path, cloud_gate.read(path)) for path in sorted(previous.glob('*/request.json'))]
        if requests and all(ledger.instant(row, 'expires') <= worker.clock() for _, row in requests):
            due = [path for path, row in requests if ledger.text(row, 'attempt') in unresolved]
            if due:
                _ = cloud_gate.cleanup_requests(due, worker)


def paired_cleanup_main(argv: Sequence[str], settings_path: Path, directory: Path) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument('--paired-cleanup', type=Path, required=True)
    class CleanupArguments(argparse.Namespace):
        paired_cleanup: Path = Path()
    args = parser.parse_args(list(argv), namespace=CleanupArguments())
    if os.environ.get('SERVICE_RESULT') == 'timeout':
        cloud_gate.save(args.paired_cleanup / SERVICE_TIMEOUT_FILE, {'wait': TrialWait.SERVICE_RUNTIME.name})
    policy = conf.read(settings_path)
    worker = cloud_gate.GateWorker(AwsCli(policy.text('credentials_profile'), policy.text('region'),
        cloud_gate.settings().number('api_timeout_s')), trial_ledger(directory), settings_path,
        args.paired_cleanup / 'reservation.cgroup', clock=time.time)
    with cancellation.defer_cleanup_signals():
        try:
            return int(not cleanup_trial(args.paired_cleanup, worker))
        except TrialWaitExpired as error:
            print('paired trial: ' + str(error), file=sys.stderr)
            return 1


class TrialMessage(Enum):
    READY = b'R'
    LAUNCH = b'L'
    ABORT = b'A'


class LaunchBarrier(Protocol):
    def send(self, message: TrialMessage, /) -> None: ...
    def recv(self) -> TrialMessage: ...
    def poll(self, timeout: float) -> bool: ...
    def close(self) -> None: ...


class TrialChannel:
    """Send one byte per message so a partial frame cannot block a reader."""

    def __init__(self, channel: socket.socket) -> None:
        self.channel: socket.socket = channel
        self.channel.setblocking(False)

    def fileno(self) -> int:
        return self.channel.fileno()

    def send(self, message: TrialMessage, /) -> None:
        self.channel.sendall(message.value)

    def recv(self) -> TrialMessage:
        message = self.channel.recv(1)
        if not message:
            raise EOFError
        return TrialMessage(message)

    def poll(self, timeout: float) -> bool:
        return bool(wait([self.fileno()], timeout=timeout))

    def close(self) -> None:
        self.channel.close()


def paired_module(folder: Path, directory: Path, settings_path: Path, connection: LaunchBarrier, *,
                  group: CloudGroup) -> None:
    """Keep one queue slot while the shared worker owns its box."""
    try:
        policy = conf.read(settings_path)
        settings = cloud_gate.settings()
        worker = cloud_gate.GateWorker(AwsCli(policy.text('credentials_profile'), policy.text('region'),
            settings.number('api_timeout_s')), trial_ledger(directory), settings_path,
            folder / 'reservation.cgroup', clock=time.time)
        # The coordinator stops this worker if the queue does not grant admission by its deadline.
        with redirect_stdout(sys.stderr), cloud_gate.cloud_reservation(
                folder, worker.sidecar, PAIRED_TRIAL_JOB, group=group) as slot:
            try:
                connection.send(TrialMessage.READY)
                try:
                    launch_timeout = settings.number('paired_admission_timeout_s') + preparation_seconds(settings, group.size)
                    if not connection.poll(launch_timeout):
                        raise TrialWaitExpired(TrialWait.LAUNCH)
                    decision = connection.recv()
                except EOFError:
                    return
                if decision is not TrialMessage.LAUNCH:
                    return
                if (folder / 'outcome.json').exists():
                    return
                # The coordinator sets the box expiry at launch, so read the request now.
                request = cloud_gate.read(folder / 'request.json')
                deployment = cloud_gate.read(folder / 'deployment.json')
                outcome = worker.execute(folder, request, deployment)
                cloud_gate.save(folder / 'outcome.json', outcome)
                slot.status = int(outcome.get('status') != 'complete')
            finally:
                with cancellation.defer_cleanup_signals():
                    if not cleanup_trial(folder, worker):
                        slot.status = 1
    except BaseException as error:
        outcome: dict[str, object] = {'status': 'fallback', 'reason': str(error)}
        if isinstance(error, TrialWaitExpired):
            outcome['wait'] = error.wait_for.name
            print(f'paired trial: {folder.name}: {error}', file=sys.stderr)
        cloud_gate.save(folder / 'outcome.json', outcome)
    finally:
        connection.close()


def _start_paired_module(folder: Path, directory: Path, settings_path: Path, connection: LaunchBarrier, *,
                         group: CloudGroup, coordinator_ends: tuple[LaunchBarrier, ...]) -> None:
    # Only the coordinator keeps these ends so its exit sends EOF to each worker.
    for end in coordinator_ends:
        end.close()
    paired_module(folder, directory, settings_path, connection, group=group)


def join_workers(processes: Sequence[BaseProcess], deadline: TrialDeadline) -> list[BaseProcess]:
    remaining: list[BaseProcess] = []
    for process in processes:
        process.join(timeout=deadline.remaining())
        if process.exitcode is None:
            remaining.append(process)
    return remaining


def stop_workers(processes: Sequence[BaseProcess], grace_timeout: float, reap_timeout: float) -> None:
    for process in processes:
        if process.is_alive():
            process.terminate()
    remaining = join_workers(processes, TrialDeadline.after(grace_timeout))
    for process in remaining:
        process.kill()
    unreaped = join_workers(remaining, TrialDeadline.after(reap_timeout))
    for process in processes:
        if process not in unreaped:
            process.close()
    if unreaped:
        raise TrialWaitExpired(TrialWait.WORKER_REAP)


def coordinate_trial(folder: Path, directory: Path, settings_path: Path) -> int:
    """Launch all modules after every slot and cost reservation succeeds."""
    started_at = time.time()
    settings = cloud_gate.settings()
    admission_timeout = settings.number('paired_admission_timeout_s')
    api_timeout = settings.number('api_timeout_s')
    cleanup_timeout = cleanup_seconds(settings)
    lifetime = settings.whole('lifetime_minutes') * 60
    policy = conf.read(settings_path)
    book = trial_ledger(directory)
    worker = cloud_gate.GateWorker(AwsCli(policy.text('credentials_profile'), policy.text('region'),
        cloud_gate.settings().number('api_timeout_s')), book, settings_path, folder / 'reservation.cgroup', clock=time.time)
    requests = [cloud_gate.read(path) for path in sorted(folder.glob('*/request.json'))]
    children: list[tuple[BaseProcess, LaunchBarrier]] = []
    workers: dict[int, tuple[str, LaunchBarrier]] = {}
    waiting: set[int] = set()
    launched_count = 0
    abort_sent = False
    failed = False
    fee_path = folder / 'query-cost.json'
    query_fee = Decimal(str(cloud_gate.read(fee_path)['usd'])) if fee_path.exists() else Decimal(0)
    def record_query(fee: Decimal) -> None:
        nonlocal query_fee
        query_fee += fee
        cloud_gate.save(fee_path, {'usd': str(query_fee)})

    def check_workers(wait_for: TrialWait = TrialWait.LAUNCH) -> None:
        # A worker sends no more messages after it reports ready.
        for descriptor in wait(list(workers), timeout=0):
            assert isinstance(descriptor, int)
            attempt, _connection = workers[descriptor]
            raise GateRefused(f'{attempt}: The module ended before launch')
        preparation.check(wait_for)

    def abort_unlaunched() -> None:
        nonlocal abort_sent
        if abort_sent or launched_count == len(children):
            return
        abort_sent = True
        pending = children[launched_count:]
        for process, connection in pending:
            if process.is_alive():
                try:
                    connection.send(TrialMessage.ABORT)
                except (OSError, ValueError, EOFError):
                    pass
        if not waiting:
            _ = join_workers([process for process, _ in pending], TrialDeadline.after(cleanup_timeout))
        for process, _connection in pending:
            if process.is_alive():
                process.terminate()

    try:
        sweep_trial_resources(folder, worker)
        pid = os.getpid()
        start = process_start_time(pid)
        if start is None:
            raise GateRefused('The trial coordinator has no live process identity')
        context = multiprocessing.get_context('fork')
        for request in requests:
            attempt = ledger.text(request, 'attempt')
            parent_socket, child_socket = socket.socketpair()
            parent, child = TrialChannel(parent_socket), TrialChannel(child_socket)
            coordinator_ends = tuple(connection for _, connection in children) + (parent,)
            process = context.Process(target=_start_paired_module,
                args=(folder / attempt, directory, settings_path, child),
                kwargs={'group': CloudGroup(folder.name, len(requests), started_at, Owner(pid, start)),
                        'coordinator_ends': coordinator_ends})
            try:
                process.start()
            except BaseException:
                parent.close()
                raise
            finally:
                child.close()
            children.append((process, parent))
            descriptor = parent.fileno()
            workers[descriptor] = (attempt, parent)
            waiting.add(descriptor)
        admission = TrialDeadline.after(admission_timeout)
        while waiting:
            admission.check(TrialWait.ADMISSION)
            ready = wait(list(workers), timeout=admission.remaining())
            if not ready:
                raise TrialWaitExpired(TrialWait.ADMISSION)
            for descriptor in ready:
                assert isinstance(descriptor, int)
                attempt, connection = workers[descriptor]
                if descriptor not in waiting:
                    raise GateRefused(f'{attempt}: The module ended before launch')
                try:
                    if connection.recv() is TrialMessage.READY:
                        waiting.remove(descriptor)
                        continue
                except EOFError:
                    pass
                outcome = folder / attempt / 'outcome.json'
                reason = cloud_gate.read(outcome).get('reason') if outcome.exists() else None
                raise GateRefused(f'{attempt}: {reason or "The module exited before cloud admission"}')
        preparation = TrialDeadline.after(preparation_seconds(settings, len(requests)))
        # The lock protects the batch of cost reservations across trials.
        with trial_lock(directory / 'paired-trial-slots' / 'controller.lock', TrialDeadline.after(api_timeout),
                        TrialWait.RESERVATION_LOCK):
            pending = [request for request in requests
                       if not (folder / ledger.text(request, 'attempt') / 'outcome.json').exists()]
            # Set the box expiry after admission and before reserving costs.
            expires = int(worker.clock()) + lifetime
            worker_exit = TrialDeadline.after(expires - worker.clock() + cleanup_timeout)
            for request in pending:
                request['expires'] = expires
                cloud_gate.save(folder / ledger.text(request, 'attempt') / 'request.json', request)
            reserve_trial(worker, pending, record_query, check_workers=check_workers,
                          before_charge_page=lambda: check_workers(TrialWait.COST_REFRESH))
        check_workers()
        try:
            for _process, connection in children:
                connection.send(TrialMessage.LAUNCH)
                launched_count += 1
        except (OSError, ValueError, EOFError) as error:
            print('paired trial: ' + str(error), file=sys.stderr)
            failed = True
            with cancellation.defer_cleanup_signals():
                abort_unlaunched()
        if join_workers([process for process, _ in children[:launched_count]], worker_exit):
            raise TrialWaitExpired(TrialWait.WORKER_EXIT)
    except (OSError, ValueError, EOFError, GateRefused, AwsUnavailable, subprocess.SubprocessError,
            KeyboardInterrupt) as error:
        print('paired trial: ' + str(error), file=sys.stderr)
        failed = True
    finally:
        with cancellation.defer_cleanup_signals():
            abort_unlaunched()
            for _, connection in children:
                connection.close()
            reap_failed = False
            try:
                stop_workers([process for process, _ in children], api_timeout, cleanup_timeout)
            except TrialWaitExpired as error:
                reap_failed = True
                with suppress(OSError, ValueError):
                    print('paired trial: ' + str(error), file=sys.stderr, flush=True)
            finally:
                try:
                    cleanup_confirmed = cleanup_trial(folder, worker)
                except TrialWaitExpired as error:
                    cleanup_confirmed = False
                    with suppress(OSError, ValueError):
                        print('paired trial: ' + str(error), file=sys.stderr)
                finally:
                    if reap_failed:
                        # Python can wait for an unreaped worker during shutdown.
                        try:
                            for stream in (sys.stdout, sys.stderr):
                                with suppress(OSError, ValueError):
                                    _ = stream.flush()
                        finally:
                            os._exit(1)
    summaries: list[dict[str, object]] = []
    cursor = folder / 'emitted.json'
    emitted = cloud_gate.read(cursor) if cursor.exists() else {}
    for request in requests:
        attempt = ledger.text(request, 'attempt')
        path = folder / attempt
        runs: list[dict[str, object]] = []
        if (path / cloud_gate.RESULT_FILE).exists():
            row = cloud_gate.read(path / cloud_gate.RESULT_FILE)
            launch = cloud_gate.read(path / 'launch.json')
            try:
                validate_paired_result(row, request, ledger.text(launch, 'instance'))
                runs = list(ledger.rows(row, 'runs'))
            except (ValueError, KeyError, TypeError) as error:
                print('paired trial: ' + str(error), file=sys.stderr)
        if not runs and (path / 'gate.log').exists():
            # A stopped guest can leave complete samples before its final reply.
            for line in (path / 'gate.log').read_text().splitlines():
                try:
                    sample = ledger.mapping(cast(object, json.loads(line)))
                    partial = {**request, 'runs': [*runs, sample], 'verdict': 'fail'}
                    launch = cloud_gate.read(path / 'launch.json')
                    validate_paired_result(partial, request, ledger.text(launch, 'instance'))
                    runs.append(sample)
                except (ValueError, KeyError, TypeError):
                    continue
        for sample in runs:
            identity = f"{attempt}:{sample['variant']}:{sample['round']}"
            if identity not in emitted:
                print(json.dumps(sample), flush=True)
                emitted[identity] = True
                cloud_gate.save(cursor, emitted)
        summary = module_summary(ledger.text(request, 'module'), ledger.instant(request, 'rounds'), runs)
        failed = failed or summary['all_passed'] is not True
        summaries.append(summary)
    for summary in summaries:
        print(json.dumps(summary), flush=True)
    cost, cleanup = trial_cost(book, {ledger.text(row, 'attempt') for row in requests}, settings_path, int(time.time()))
    cleanup = cleanup and cleanup_confirmed
    cost += query_fee
    print(json.dumps({'total_cost_usd': float(cost), 'cost_basis': 'estimate', 'cleanup_complete': cleanup}), flush=True)
    return int(failed or not cleanup)


def paired_worker_main(argv: Sequence[str], settings_path: Path, directory: Path) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument('--paired-worker', type=Path, required=True)
    class WorkerArguments(argparse.Namespace):
        paired_worker: Path = Path()
    args = parser.parse_args(list(argv), namespace=WorkerArguments())
    return coordinate_trial(args.paired_worker, directory, settings_path)


def paired_service(command: Sequence[str], unit: str) -> int:
    """Bound the terminal wait and stop only the service for this trial."""
    settings = cloud_gate.settings()
    cleanup_timeout = cleanup_seconds(settings)
    api_timeout = settings.number('api_timeout_s')
    stop_timeout = 2 * cleanup_timeout + api_timeout
    runtime = (settings.number('paired_admission_timeout_s') + 60 * settings.whole('lifetime_minutes')
               + preparation_seconds(settings, settings.whole('slots')) + 3 * cleanup_timeout + api_timeout)
    command = [command[0], f'--property=RuntimeMaxSec={runtime:g}', *command[1:]]
    completed = False
    failure = ''
    try:
        status = trial_command(command, TrialWait.SERVICE, timeout=runtime + stop_timeout).returncode
        completed = True
    except KeyboardInterrupt:
        return 1
    except BaseException as error:
        failure = str(error)
        raise
    finally:
        if not completed:
            with cancellation.defer_cleanup_signals():
                try:
                    stop = trial_command(['systemctl', '--user', 'stop', unit],
                                         TrialWait.SERVICE_STOP, timeout=stop_timeout)
                    if stop.returncode:
                        raise TrialServiceStopUnconfirmed(stop.returncode)
                except (OSError, TrialWaitExpired, TrialServiceStopUnconfirmed):
                    if failure:
                        print('paired trial: ' + failure, file=sys.stderr)
                    raise
    folder = Path(command[command.index('--paired-worker') + 1])
    if (folder / SERVICE_TIMEOUT_FILE).exists():
        raise TrialWaitExpired(TrialWait.SERVICE_RUNTIME)
    return status


def paired_main(argv: Sequence[str], directory: Path) -> int:
    parser = argparse.ArgumentParser(description='Compare two local revisions on disposable gate boxes',
                                     epilog='aws_trial.py pushes and deletes its own trial branches; never push or delete a trial branch by hand.')
    _ = parser.add_argument('--base', required=True)
    _ = parser.add_argument('--changed', required=True)
    _ = parser.add_argument('--modules', nargs='+', required=True)
    _ = parser.add_argument('--rounds', type=int, default=3)
    _ = parser.add_argument('--repo', type=Path, default=Path.home() / '.claude')
    class PairedArguments(argparse.Namespace):
        base: str = ''
        changed: str = ''
        modules: list[str] = []
        rounds: int = 3
        repo: Path = Path()
    args = parser.parse_args(list(argv), namespace=PairedArguments())
    trial_id = ''
    prefix = ''
    clone_url = ''
    expires = 0
    service_attempted = False
    status = 1
    try:
        modules = args.modules
        if len(modules) > cloud_gate.settings().whole('slots'):
            raise GateRefused('The trial needs more cloud slots than configured')
        trial = trial_request(args.repo, args.base, args.changed, modules, args.rounds)
        home = conf.read(account_settings_path()).text('region')
        _ = gate_regions(home)
        deployment = gate_deployment(directory / 'gate-deployment.json', home)
        clone_url = trial.clone_url
        prefix = cloud_gate.settings().text('trial_branch_prefix')
        sweep_trial_branches(args.repo, clone_url, prefix, int(time.time()))
        expires = int(time.time()) + cloud_gate.settings().whole('lifetime_minutes') * 60
        folder = directory / 'paired-trials' / uuid4().hex
        folder.mkdir(parents=True)
        trial_id = folder.name
        for variant, revision in (('base', trial.base), ('changed', trial.changed)):
            branch = f'{prefix}-{expires}-{folder.name}-{variant}'
            _ = checked_git(args.repo, 'check-ref-format', 'refs/heads/' + branch)
            _ = checked_git(args.repo, 'push', clone_url, revision.commit + ':refs/heads/' + branch)
        for module in trial.modules:
            path = folder / uuid4().hex
            path.mkdir()
            cloud_gate.save(path / 'request.json', module_request(trial, module, path.name, expires))
            cloud_gate.save(path / 'deployment.json', deployment)
        command = cloud_gate.service_command('scripts.aws_target.aws_trial', ['--paired-worker', str(folder)],
                                             'cloud-paired-trial-' + folder.name, paired=True)
        service_attempted = True
        status = paired_service(command, 'cloud-paired-trial-' + folder.name)
    except (TrialWaitExpired, TrialServiceStopUnconfirmed) as error:
        print('paired trial: ' + str(error), file=sys.stderr)
        print(json.dumps({'total_cost_usd': 0, 'cost_basis': 'estimate', 'cleanup_complete': not service_attempted}))
    except (OSError, ValueError, GateRefused, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print('paired trial: ' + str(error), file=sys.stderr)
        print(json.dumps({'total_cost_usd': 0, 'cost_basis': 'estimate', 'cleanup_complete': True}))
    finally:
        with cancellation.defer_cleanup_signals():
            if trial_id:
                try:
                    delete_trial_branches(args.repo, clone_url, prefix, expires, trial_id)
                except TrialWaitExpired:
                    status = 1
    return status


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:], account_settings_path(), target_config.ssh_target_state_directory('aws')))

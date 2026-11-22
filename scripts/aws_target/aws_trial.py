"""Compare local revisions on disposable gate instances."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Sequence
from contextlib import ExitStack, redirect_stdout, suppress
from dataclasses import asdict, dataclass
from decimal import Decimal
from datetime import UTC, datetime
import json
from math import isfinite
import multiprocessing
import os
import re
import sys
from multiprocessing.connection import wait
from multiprocessing.process import BaseProcess
from pathlib import Path
import select
import signal
import statistics
import subprocess
from tempfile import TemporaryFile
import time
from typing import Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from scripts.aws_target.gate_box import (
    GUEST as GUEST, user_data as user_data,
    boot_trap as boot_trap, log_push_loop as log_push_loop,
    RUN_FIELDS as RUN_FIELDS, validate_paired_result as validate_paired_result,
)
from scripts.aws_target import ledger, cloud_gate
from scripts.aws_target.s3_presign import SigningKey as SigningKey, presign as presign
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable, control_lock
from scripts.aws_target.config import read_settings, account_settings_path, gate_deployment, gate_regions
from scripts.lib import conf
from scripts.lib.boot_identity import boot_identity
from scripts.lib.file_lock import LockResult, exclusive
from scripts.build_queue import program_start, queue_place, snapshot_codec
from scripts.build_queue.admission_jobs import StepJob
from scripts.lib.process_start import Started, Unreadable, process_start
from scripts.lib.time_limit import TIMED_OUT
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


def checked_git(repo: Path, *arguments: str, recovery: Path | None = None) -> str:
    if arguments and arguments[0] in ('push', 'ls-remote'):
        if recovery is None:
            raise ValueError('Network Git requires a recovery record')
        return network_git(repo, *arguments, recovery=recovery)
    command = ['git', '-C', str(repo), *arguments]
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.PIPE,
                                       env={**os.environ, 'GIT_NO_LAZY_FETCH': '1'}).strip()
    except subprocess.CalledProcessError as error:
        error_text = cast(object, error.stderr)
        detail = error_text.strip() if isinstance(error_text, str) else str(error)
        raise GateRefused(f'Local Git failed in {repo}: {detail}') from error


GIT_RECOVERY = 'git-recovery.json'
TRIAL_COST_RECORD = 'trial-cost.json'


def network_git(repo: Path, *arguments: str, recovery: Path) -> str:
    """Run a network Git command with a record and one deadline."""
    timeout = cloud_gate.settings().number('git_network_timeout_s')
    deadline = time.monotonic() + timeout
    stop_seconds = conf.read('build_admission').number('program_stop_seconds')
    return supervised_git(['git', '-C', str(repo), *arguments], recovery, deadline, stop_seconds)


def poll_child(child: subprocess.Popen[bytes], deadline: float, lifeline: int | None = None) -> int | None:
    """Check for child exit until the deadline or controller exit."""
    while True:
        status = child.poll()
        if status is not None:
            return status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        delay = min(0.05, remaining)
        if lifeline is None:
            time.sleep(delay)
        elif select.select([lifeline], [], [], delay)[0]:
            return None


def supervised_git(command: Sequence[str], recovery: Path, deadline: float, stop_seconds: float) -> str:
    """Close the supervisor's lifeline when the controller stops waiting."""
    started = time.monotonic()
    with TemporaryFile() as output, TemporaryFile() as errors:
        reader, writer = os.pipe()
        try:
            try:
                supervisor = subprocess.Popen([sys.executable, '-m', 'scripts.aws_target.aws_trial',
                    '--supervise-git', str(reader), str(recovery.absolute()), str(deadline), str(stop_seconds),
                    os.getcwd(), *command],
                    cwd=Path(__file__).resolve().parents[2], pass_fds=(reader,), start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=errors)
            finally:
                os.close(reader)
            try:
                status = poll_child(supervisor, deadline)
            except BaseException:
                os.close(writer)
                writer = -1
                if poll_child(supervisor, min(deadline, time.monotonic() + stop_seconds)) is None:
                    print(f'UNRESOLVED: Git supervisor did not exit for {recovery}', file=sys.stderr)
                raise
        finally:
            if writer != -1:
                os.close(writer)
        if status is None:
            raise GateRefused(f'UNRESOLVED: Git supervisor exceeded its deadline for {recovery}')
        _ = errors.seek(0)
        error_text = errors.read().decode(errors='replace')
        if recovery.exists():
            raise GateRefused(f'UNRESOLVED: Git recovery kept {recovery}\n{error_text.strip()}')
        _ = output.seek(0)
        result = output.read().decode(errors='replace')
    if status == TIMED_OUT:
        raise subprocess.TimeoutExpired(command, max(0, deadline - started), output=result, stderr=error_text)
    if status:
        raise subprocess.CalledProcessError(status, command, output=result, stderr=error_text)
    return result.strip()


@dataclass(frozen=True)
class GitRecovery:
    boot: str
    owner: int
    owner_start: str
    unit: str
    deadline: float

    @classmethod
    def read(cls, path: Path) -> GitRecovery:
        try:
            record = cloud_gate.read(path)
            boot = ledger.text(record, 'boot')
            unit = ledger.text(record, 'unit')
            owner = ledger.instant(record, 'owner')
            deadline = record.get('deadline')
            if (record.keys() != cls.__dataclass_fields__.keys()
                    or str(UUID(boot)) != boot
                    or not re.fullmatch(r'cloud-trial-git-[0-9a-f]{32}\.scope', unit)
                    or not 0 < owner < 2 ** 31 or type(deadline) is not float
                    or not isfinite(deadline) or deadline <= 0):
                raise ValueError('invalid Git recovery fields')
            return cls(boot, owner, ledger.text(record, 'owner_start'), unit, deadline)
        except (ValueError, RecursionError) as error:
            raise GateRefused(f'UNRESOLVED: invalid Git recovery record {path}: {error}') from error


def clear_git_record(path: Path) -> None:
    path.unlink(missing_ok=True)
    path.with_suffix('.scope.json').unlink(missing_ok=True)
    path.with_suffix('.cgroup').unlink(missing_ok=True)


def bounded_systemctl(arguments: Sequence[str], deadline: float) -> tuple[int, str] | None:
    """Run systemctl without waiting past the deadline to reap it."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    kill_at = deadline - min(0.05, remaining / 2)
    with TemporaryFile() as output:
        child = subprocess.Popen(['systemctl', '--user', *arguments], stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.DEVNULL)
        try:
            status = poll_child(child, kill_at)
            if status is not None:
                _ = output.seek(0)
                return status, output.read().decode()
            return None
        finally:
            if child.poll() is None:
                child.kill()
                _ = poll_child(child, deadline)


def query_git_scope(unit: str, deadline: float) -> Path | None:
    """Return the unit's cgroup or confirm that the unit is absent."""
    try:
        result = bounded_systemctl(['show', '--all',
                                    '--property=LoadState,ActiveState,ControlGroup,Job', '--', unit], deadline)
    except (OSError, UnicodeError) as error:
        raise GateRefused(f'UNRESOLVED: cannot read Git scope {unit}: {error}') from error
    if result is not None:
        status, output = result
        fields = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        group = fields.get('ControlGroup', '')
        if status == 0 and fields.get('LoadState') == 'loaded' and group.startswith('/'):
            return Path('/sys/fs/cgroup') / group.lstrip('/')
        if (status in (0, 1) and fields.get('LoadState') == 'not-found'
                and fields.get('ActiveState') == 'inactive' and fields.get('ControlGroup') == ''
                and fields.get('Job') in ('', '0')):
            return None
    raise GateRefused(f'UNRESOLVED: cannot determine the state of Git scope {unit}')


def stop_git_scope(path: Path, record: GitRecovery, deadline: float, sample_seconds: float) -> None:
    published = queue_place.read_program_start(path.with_suffix('.scope.json'))
    if published is None:
        scope = query_git_scope(record.unit, deadline)
        if scope is None:
            return
    else:
        if (published.holder.pid, published.holder.start_time) != (record.owner, record.owner_start):
            raise GateRefused(f'UNRESOLVED: Git scope holder does not match {path}')
        scope = Path(published.scope)
    if scope.name != record.unit or '..' in scope.parts or not scope.is_relative_to('/sys/fs/cgroup'):
        raise GateRefused(f'UNRESOLVED: cannot verify the Git scope in {path}')
    try:
        queue_place.stop_scope(scope, deadline, sample_seconds)
    except queue_place.CleanupUnresolved as error:
        raise GateRefused(str(error)) from error


def recover_git(path: Path, stop_seconds: float, sample_seconds: float) -> None:
    """Stop a recorded scope after its supervisor exits."""
    with exclusive(path.with_suffix('.lock'), wait=False) as lock:
        if lock is LockResult.BUSY:
            return
        try:
            record = GitRecovery.read(path)
        except FileNotFoundError:
            return
        if record.boot == boot_identity():
            owner = process_start(record.owner)
            if isinstance(owner, Unreadable):
                raise GateRefused(f'UNRESOLVED: cannot verify the Git supervisor in {path}')
            if isinstance(owner, Started) and owner.start == record.owner_start:
                return
            stop_git_scope(path, record, time.monotonic() + stop_seconds, sample_seconds)
        clear_git_record(path)


def supervise_git(lifeline: int, path: Path, deadline: float, stop_seconds: float, cwd: Path,
                  command: Sequence[str]) -> int:
    """Keep the recovery record locked through scope cleanup."""
    sample_seconds = conf.read('build_admission').number('program_sample_seconds')
    with exclusive(path.with_suffix('.lock'), wait=False) as lock:
        if lock is LockResult.BUSY or path.exists():
            raise GateRefused(f'UNRESOLVED: Git recovery still owns {path}')
        if time.monotonic() >= deadline or select.select([lifeline], [], [], 0)[0]:
            return TIMED_OUT
        clear_git_record(path)
        owner = process_start(os.getpid())
        if not isinstance(owner, Started):
            raise GateRefused('UNRESOLVED: Git supervisor has no process identity')
        record = GitRecovery(boot_identity(), os.getpid(), owner.start,
                             f'cloud-trial-git-{uuid4().hex}.scope', deadline)
        cloud_gate.save(path, asdict(record))
        return start_scoped_git(lifeline, path, record, stop_seconds, sample_seconds, cwd, command)


def start_scoped_git(lifeline: int, path: Path, record: GitRecovery, stop_seconds: float,
                     sample_seconds: float, cwd: Path, command: Sequence[str]) -> int:
    # Reserve time for cleanup within the same deadline.
    remaining = max(0, record.deadline - time.monotonic())
    run_deadline = record.deadline - min(stop_seconds, remaining / 2)
    child: subprocess.Popen[bytes] | None = None
    status = None
    try:
        with ExitStack() as barrier:
            reader, writer = os.pipe()
            _ = barrier.callback(os.close, reader)
            _ = barrier.callback(os.close, writer)
            wrapper = Path(program_start.__file__).resolve()
            argv = ['systemd-run', '--user', '--scope', '--quiet', '--collect',
                    '--expand-environment=no', '--slice=builds.slice', '--unit=' + record.unit, '--',
                    sys.executable, str(wrapper), '--scope-start', '--authorized', str(path.with_suffix('.cgroup')),
                    str(record.owner), record.owner_start, str(path.with_suffix('.scope.json')), str(reader),
                    str(max(0, run_deadline - time.monotonic())), *command]
            child = subprocess.Popen(argv, cwd=cwd, pass_fds=(reader,), stdin=subprocess.DEVNULL)
            if time.monotonic() < run_deadline and not select.select([lifeline], [], [], 0)[0]:
                with suppress(BrokenPipeError):
                    _ = os.write(writer, b'1')
        status = poll_child(child, run_deadline, lifeline)
    finally:
        stop_deadline = min(record.deadline, time.monotonic() + stop_seconds)
        if child is not None and status is None and not path.with_suffix('.scope.json').exists():
            # The launcher can still create the scope until it exits.
            child.kill()
            if poll_child(child, stop_deadline) is None:
                raise GateRefused(f'UNRESOLVED: Git scope launcher did not exit for {path}')
        stop_git_scope(path, record, stop_deadline, sample_seconds)
        if child is not None and status is None and poll_child(child, stop_deadline) is None:
            raise GateRefused(f'UNRESOLVED: Git scope launcher did not exit for {path}')
        clear_git_record(path)
    return status if status is not None else TIMED_OUT


def trial_request(repo: Path, base: str, changed: str, modules: Sequence[str], rounds: int) -> PairedTrialRequest:
    """Resolve local commits without changing the source checkout."""
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


def delete_trial_branches(repo: Path, clone_url: str, prefix: str, expires: int, folder: Path) -> None:
    """Find and delete remote branches in this trial's unique namespace."""
    trial_id = folder.name
    namespace = f'refs/heads/{prefix}-{expires}-{trial_id}-'
    recovery = folder / GIT_RECOVERY
    if recovery.exists():
        return
    try:
        remote = checked_git(repo, 'ls-remote', '--heads', clone_url, namespace + '*', recovery=recovery)
    except GateRefused as error:
        print(error, file=sys.stderr)
        return
    except (OSError, ValueError, subprocess.SubprocessError):
        print(f'paired trial: could not list branches for trial {trial_id}', file=sys.stderr)
        return
    for line in remote.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[1].startswith(namespace):
            continue
        branch = fields[1]
        try:
            _ = checked_git(repo, 'push', clone_url, '--delete', branch, recovery=recovery)
        except GateRefused as error:
            print(error, file=sys.stderr)
            return
        except (OSError, ValueError, subprocess.SubprocessError):
            print(f'paired trial: could not delete branch {branch.removeprefix("refs/heads/")}', file=sys.stderr)


def sweep_trial_branches(repo: Path, clone_url: str, prefix: str, now: int, folder: Path) -> None:
    """Delete expired trial branches without blocking a new trial on failure."""
    namespace = f'refs/heads/{prefix}-'
    recovery = folder / GIT_RECOVERY
    try:
        remote = checked_git(repo, 'ls-remote', '--heads', clone_url, namespace + '*', recovery=recovery)
        for line in remote.splitlines():
            fields = line.split()
            if len(fields) != 2 or not fields[1].startswith(namespace):
                continue
            name = fields[1].removeprefix(namespace)
            match = re.fullmatch(r'([0-9]{1,12})-([0-9a-f]{32})-(base|changed)', name)
            if match and int(match[1]) < now and not (folder.parent / match[2] / GIT_RECOVERY).exists():
                _ = checked_git(repo, 'push', clone_url, '--delete', fields[1], recovery=recovery)
    except (OSError, ValueError, subprocess.SubprocessError):
        print(f'paired trial: could not sweep old branches for {prefix}', file=sys.stderr)


def module_request(trial: PairedTrialRequest, module: str, attempt: str, expires: int) -> dict[str, object]:
    return {'task': 'paired', 'attempt': attempt, 'base': asdict(trial.base), 'changed': asdict(trial.changed),
            'commit': trial.changed.commit, 'tree': trial.changed.tree, 'head': trial.changed.commit,
            'origin': trial.clone_url, 'clone_url': trial.clone_url, 'mode': 'targeted-test',
            'module': module, 'modules': ['scripts/' + module], 'rounds': trial.rounds, 'source_key': '',
            'expires': expires, 'gate_job': snapshot_codec.snapshot_value(PAIRED_TRIAL_JOB),
            'module_time_limit_s': cloud_gate.settings().number('module_time_limit_s'),
            'delivery_s': cloud_gate.cleanup_timeout_s()}


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
                  check_workers: Callable[[], None] = lambda: None) -> None:
    """Reserve every box before the coordinator permits any launch."""
    check_workers()
    worker.reconcile()
    check_workers()
    worker.charges(record_query)
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
    policy = conf.read(settings_path)
    worker = cloud_gate.GateWorker(AwsCli(policy.text('credentials_profile'), policy.text('region'),
        cloud_gate.settings().number('api_timeout_s')), ledger.CostLedger(directory), settings_path,
        args.paired_cleanup / 'reservation.cgroup', clock=time.time)
    return int(not cleanup_trial(args.paired_cleanup, worker))


class LaunchBarrier(Protocol):
    def send(self, obj: object, /) -> None: ...
    def recv(self) -> object: ...
    def close(self) -> None: ...


def paired_module(folder: Path, directory: Path, settings_path: Path, connection: LaunchBarrier, *,
                  group: CloudGroup) -> None:
    """Keep one queue slot while the shared worker owns its box."""
    policy = conf.read(settings_path)
    worker = cloud_gate.GateWorker(AwsCli(policy.text('credentials_profile'), policy.text('region'),
        cloud_gate.settings().number('api_timeout_s')), ledger.CostLedger(directory), settings_path, folder / 'reservation.cgroup', clock=time.time)
    outcome_path = folder / 'outcome.json'
    try:
        with redirect_stdout(sys.stderr), cloud_gate.cloud_reservation(
                folder, worker.sidecar, PAIRED_TRIAL_JOB, group=group) as slot:
            try:
                connection.send('ready')
                try:
                    decision = connection.recv()
                except EOFError:
                    return
                if decision != 'launch':
                    return
                if outcome_path.exists():
                    return
                # The coordinator sets the box expiry at launch, so read the request now.
                request = cloud_gate.read(folder / 'request.json')
                deployment = cloud_gate.read(folder / 'deployment.json')
                outcome = worker.execute(folder, request, deployment)
                cloud_gate.save(outcome_path, outcome)
                slot.status = int(outcome.get('status') != 'complete')
            finally:
                slot.keep = True
                if cleanup_trial(folder, worker):
                    slot.keep = False
                else:
                    slot.status = 1
    except cloud_gate.CloudReleaseUnresolved as error:
        saved = cloud_gate.read(outcome_path) if outcome_path.exists() else {}
        if saved.get('status') == 'complete':
            print(error, file=sys.stderr, flush=True)
            cleanup = '\n'.join(filter(None, (str(saved.get('cleanup', '')), str(error))))
            cloud_gate.save(outcome_path, {**saved, 'cleanup': cleanup})
        else:
            cloud_gate.save(outcome_path, {'status': 'fallback', 'reason': str(error)})
    except BaseException as error:
        cloud_gate.save(outcome_path, {'status': 'fallback', 'reason': str(error)})
    finally:
        connection.close()


class UnresolvedTrialWorkers(GateRefused):
    """The service still owns workers that did not exit."""


def _start_paired_module(folder: Path, directory: Path, settings_path: Path, connection: LaunchBarrier, *,
                         group: CloudGroup, coordinator_ends: tuple[LaunchBarrier, ...]) -> None:
    # Only the coordinator keeps these ends so its exit sends EOF to each worker.
    for end in coordinator_ends:
        end.close()
    paired_module(folder, directory, settings_path, connection, group=group)


def coordinate_trial(folder: Path, directory: Path, settings_path: Path, *, report_cost: bool = True) -> int:
    """Launch all modules after every slot and cost reservation succeeds."""
    started_at = time.time()
    stop_timeout = conf.read('build_admission').number('program_stop_seconds')
    cleanup_timeout = cloud_gate.cleanup_timeout_s()
    policy = conf.read(settings_path)
    book = ledger.CostLedger(directory)
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

    def check_workers() -> None:
        # A worker sends no more messages after it reports ready.
        for descriptor in wait(list(workers), timeout=0):
            assert isinstance(descriptor, int)
            attempt, _connection = workers[descriptor]
            raise GateRefused(f'{attempt}: The module ended before launch')

    def abort_unlaunched() -> None:
        nonlocal abort_sent
        if abort_sent or launched_count == len(children):
            return
        abort_sent = True
        pending = children[launched_count:]
        for process, connection in pending:
            if process.is_alive():
                try:
                    connection.send('abort')
                except (OSError, ValueError, EOFError):
                    pass
        if not waiting:
            deadline = time.monotonic() + cleanup_timeout
            for process, _connection in pending:
                process.join(timeout=max(0, deadline - time.monotonic()))
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
            parent, child = context.Pipe()
            coordinator_ends = tuple(connection for _, connection in children) + (parent,)
            process = context.Process(target=_start_paired_module,
                args=(folder / attempt, directory, settings_path, child),
                kwargs={'group': CloudGroup(folder.name, len(requests), started_at, Owner(pid, start)),
                        'coordinator_ends': coordinator_ends})
            try:
                with child:
                    process.start()
            except BaseException:
                parent.close()
                raise
            children.append((process, parent))
            descriptor = parent.fileno()
            workers[descriptor] = (attempt, parent)
            waiting.add(descriptor)
        while waiting:
            for descriptor in wait(list(workers)):
                assert isinstance(descriptor, int)
                attempt, connection = workers[descriptor]
                if descriptor not in waiting:
                    raise GateRefused(f'{attempt}: The module ended before launch')
                try:
                    if connection.recv() == 'ready':
                        waiting.remove(descriptor)
                        continue
                except EOFError:
                    pass
                outcome = folder / attempt / 'outcome.json'
                reason = cloud_gate.read(outcome).get('reason') if outcome.exists() else None
                raise GateRefused(f'{attempt}: {reason or "The module exited before cloud admission"}')
        # The lock protects the batch of cost reservations across trials.
        with control_lock(directory / 'paired-trial-slots'):
            pending = [request for request in requests
                       if not (folder / ledger.text(request, 'attempt') / 'outcome.json').exists()]
            # The box lifetime starts at launch, so a queue wait does not shorten the run budget.
            expires = int(worker.clock()) + cloud_gate.settings().whole('lifetime_minutes') * 60
            for request in pending:
                request['expires'] = expires
                cloud_gate.save(folder / ledger.text(request, 'attempt') / 'request.json', request)
            reserve_trial(worker, pending, record_query, check_workers=check_workers)
        check_workers()
        try:
            for _process, connection in children:
                connection.send('launch')
                launched_count += 1
        except (OSError, ValueError, EOFError) as error:
            print('paired trial: ' + str(error), file=sys.stderr)
            failed = True
            abort_unlaunched()
        deadline = time.monotonic() + max(0, expires - worker.clock()) + cleanup_timeout
        for process, _connection in children[:launched_count]:
            process.join(timeout=max(0, deadline - time.monotonic()))
            if process.is_alive():
                raise GateRefused('A paired trial worker exceeded the box and cleanup deadlines')
    except (OSError, ValueError, EOFError, GateRefused, AwsUnavailable, subprocess.SubprocessError,
            KeyboardInterrupt) as error:
        print('paired trial: ' + str(error), file=sys.stderr)
        failed = True
    finally:
        abort_unlaunched()
        for process, connection in children:
            connection.close()
            if process.is_alive():
                process.terminate()
        deadline = time.monotonic() + stop_timeout
        for process, _connection in children:
            process.join(timeout=max(0, deadline - time.monotonic()))
        remaining = [process for process, _connection in children if process.is_alive()]
        for process in remaining:
            process.kill()
        deadline = time.monotonic() + stop_timeout
        for process in remaining:
            process.join(timeout=max(0, deadline - time.monotonic()))
        if any(process.is_alive() for process in remaining):
            raise UnresolvedTrialWorkers('Paired trial cleanup unresolved. Keep the trial records for service cleanup.')
        failed = not cleanup_trial(folder, worker) or failed
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
    cost += query_fee
    git_cleanup = not ((folder / GIT_RECOVERY).exists() or pending_git_recovery(directory))
    summary: dict[str, object] = {'total_cost_usd': float(cost), 'cost_basis': 'estimate',
                                  'cleanup_complete': cleanup and git_cleanup}
    if report_cost:
        print(json.dumps(summary), flush=True)
    else:
        cloud_gate.save(folder / TRIAL_COST_RECORD, summary)
    return int(failed or not cleanup)


def paired_worker_main(argv: Sequence[str], settings_path: Path, directory: Path) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument('--paired-worker', type=Path, required=True)
    class WorkerArguments(argparse.Namespace):
        paired_worker: Path = Path()
    args = parser.parse_args(list(argv), namespace=WorkerArguments())
    try:
        return coordinate_trial(args.paired_worker, directory, settings_path, report_cost=False)
    except UnresolvedTrialWorkers as error:
        print(error, file=sys.stderr, flush=True)
        # Python would join these workers again during interpreter shutdown.
        os._exit(1)


def paired_service(command: Sequence[str], unit: str) -> int:
    """Stop the service if its terminal command raises an exception."""
    completed = False
    try:
        status = subprocess.call(command)
        completed = True
        return status
    except KeyboardInterrupt:
        return 1
    finally:
        if not completed:
            try:
                deadline = time.monotonic() + cloud_gate.cleanup_timeout_s()
                stopped = bounded_systemctl(['stop', unit], deadline)
                if stopped is None or stopped[0]:
                    print(f'UNRESOLVED: paired trial service stop for {unit}', file=sys.stderr)
            except OSError as error:
                print(f'UNRESOLVED: paired trial service stop for {unit}: {error}', file=sys.stderr)


def pending_git_recovery(directory: Path) -> bool:
    return any((directory / 'paired-trials').glob(f'*/{GIT_RECOVERY}'))


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
    folder: Path | None = None
    prefix = ''
    clone_url = ''
    expires = 0
    trials = directory / 'paired-trials'
    cost: dict[str, object] = {'total_cost_usd': 0, 'cost_basis': 'estimate', 'cleanup_complete': True}
    try:
        records = sorted(trials.glob(f'*/{GIT_RECOVERY}'))
        if records:
            configuration = conf.read('build_admission')
            for record in records:
                try:
                    recover_git(record, configuration.number('program_stop_seconds'),
                                configuration.number('program_sample_seconds'))
                except (OSError, ValueError, GateRefused, KeyError, TypeError) as error:
                    reason = str(error).removeprefix('UNRESOLVED: ')
                    print(f'UNRESOLVED: Git recovery kept {record}: {reason}', file=sys.stderr)
        modules = args.modules
        if len(modules) > cloud_gate.settings().whole('slots'):
            raise GateRefused('The trial needs more cloud slots than configured')
        trial = trial_request(args.repo, args.base, args.changed, modules, args.rounds)
        home = conf.read(account_settings_path()).text('region')
        _ = gate_regions(home)
        deployment = gate_deployment(directory / 'gate-deployment.json', home)
        clone_url = trial.clone_url
        prefix = cloud_gate.settings().text('trial_branch_prefix')
        folder = trials / uuid4().hex
        folder.mkdir(parents=True)
        sweep_trial_branches(args.repo, clone_url, prefix, int(time.time()), folder)
        expires = int(time.time()) + cloud_gate.settings().whole('lifetime_minutes') * 60
        for variant, revision in (('base', trial.base), ('changed', trial.changed)):
            branch = f'{prefix}-{expires}-{folder.name}-{variant}'
            _ = checked_git(args.repo, 'check-ref-format', 'refs/heads/' + branch)
            _ = checked_git(args.repo, 'push', clone_url, revision.commit + ':refs/heads/' + branch,
                            recovery=folder / GIT_RECOVERY)
        for module in trial.modules:
            path = folder / uuid4().hex
            path.mkdir()
            cloud_gate.save(path / 'request.json', module_request(trial, module, path.name, expires))
            cloud_gate.save(path / 'deployment.json', deployment)
        command = cloud_gate.service_command('scripts.aws_target.aws_trial', ['--paired-worker', str(folder)],
                                             'cloud-paired-trial-' + folder.name, paired=True)
        cost['cleanup_complete'] = False
        status = paired_service(command, 'cloud-paired-trial-' + folder.name)
        if (folder / TRIAL_COST_RECORD).exists():
            cost = cloud_gate.read(folder / TRIAL_COST_RECORD)
        return status
    except (OSError, ValueError, GateRefused, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print('paired trial: ' + str(error), file=sys.stderr)
        return 1
    finally:
        if folder is not None and expires:
            delete_trial_branches(args.repo, clone_url, prefix, expires, folder)
        if pending_git_recovery(directory):
            cost['cleanup_complete'] = False
        print(json.dumps(cost), flush=True)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--supervise-git']:
        descriptor, record, deadline, stop, cwd, *command = sys.argv[2:]
        try:
            raise SystemExit(supervise_git(int(descriptor), Path(record), float(deadline), float(stop), Path(cwd), command))
        except (OSError, ValueError, GateRefused, subprocess.SubprocessError) as error:
            print(str(error), file=sys.stderr)
            raise SystemExit(1) from error
    raise SystemExit(main(sys.argv[1:], account_settings_path(), target_config.ssh_target_state_directory('aws')))

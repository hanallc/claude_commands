#!/usr/bin/env python3
"""Run gate modules through the Mac queue and compare their counts with natedev."""
from __future__ import annotations

import argparse
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, redirect_stdout, suppress
from dataclasses import dataclass, field
import fcntl
import io
import json
import os
import re
from pathlib import Path
import select
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING, cast, override

if __package__ in ('', None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.aws_target import cloud_gate, gate_box, ledger
from scripts.build_queue import admission_records
from scripts.buildlog import store
from scripts.copies import copies
from scripts.lib import build_folders, conf, copy_record, process_group, scratch
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_start import Started, Unreadable, Zombie, process_start
from scripts.production import merge_gate as gate
from scripts.production.affected_modules import GraphError
from scripts.production.gate_modules import GateRefused, discover_test_modules
from scripts.test_times import gate_record

if TYPE_CHECKING:
    from scripts.production.gate_routes import (
        MacGateCancellation, MacGateExcludeFileMissing, MacGateExclusions,
    )

MODULE_LIST = Path('config/mac_gate_modules.txt')
MODULE_LIST_HEADER = '# List test modules that must run on natedev because the Mac cannot run them.\n'
REMOTE_ROOT = '.local/state/mac-gate'
RESULT_PREFIX = 'mac_gate_result='
CLEANUP_ERROR_PREFIX = 'mac_gate_cleanup_error='
QUEUE_COMMAND = (sys.executable, str(Path(__file__).with_name('mac_queue_run.py')))
MAC_TIME_LIMIT_S = 3600


@dataclass(frozen=True)
class MacGateResult:
    row: dict[str, object]
    run: gate.GateRun


@dataclass(frozen=True)
class UseCallerProcessLifetime:
    """The Mac route watches its calling process for exit."""


USE_CALLER_PROCESS_LIFETIME = UseCallerProcessLifetime()


def read_modules(repo: Path) -> MacGateExclusions | MacGateExcludeFileMissing:
    """Return excluded test paths in the tested checkout."""
    from scripts.production.gate_routes import MacGateExcludeFileMissing, MacGateExclusions

    try:
        entries = {line for row in (repo / MODULE_LIST).read_text().splitlines()
                   if (line := row.partition('#')[0].strip())}
    except FileNotFoundError:
        return MacGateExcludeFileMissing()
    if not entries:
        return MacGateExclusions(())
    tracked_paths = set(gate.checked_git(repo, 'ls-files', '-z', '--cached', '--others',
                                        '--exclude-standard', '--', 'scripts').split('\0'))
    excluded = entries & tracked_paths
    return MacGateExclusions(tuple(name for _, path in discover_test_modules(repo)
                                   if (name := path.relative_to(repo).as_posix()) in excluded))


def attempt_name(folder: Path) -> str:
    attempt = folder.name
    suffix = attempt.removeprefix('mac-gate-')
    if suffix == attempt or not suffix.isascii() or not suffix.replace('_', 'a').isalnum():
        raise GateRefused('Invalid Mac gate attempt')
    return attempt


def running_owner(pid: int, start: str) -> bool:
    owner = process_start(pid)
    if isinstance(owner, Unreadable):
        raise GateRefused(f'Cannot read the Mac gate owner {pid}: {owner.reason}')
    return isinstance(owner, Started) and owner.start == start


def live_groups(groups: set[int]) -> set[int]:
    if not groups:
        return set()
    output = subprocess.check_output(['ps', '-axo', 'pgid=,stat='], text=True, timeout=5)
    rows = (line.split() for line in output.splitlines())
    return {int(group) for group, state in rows if int(group) in groups and not state.startswith('Z')}


def stop_guest(folder: Path, deadline: float) -> None:
    """Interrupt a live guest. Recover recorded resources after it exits."""
    journal = folder / 'run.json'
    if not journal.exists():
        return
    record = cloud_gate.read(journal)
    pid, start = ledger.instant(record, 'pid'), ledger.text(record, 'start')
    if pid <= 1 or pid == os.getpid():
        raise GateRefused('Invalid Mac gate owner')
    if running_owner(pid, start):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGINT)
        while running_owner(pid, start):
            if time.monotonic() >= deadline:
                raise GateRefused('The Mac gate guest did not exit')
            time.sleep(0.05)

    record = cloud_gate.read(journal)
    groups = {int(group): stamp for group, stamp in ledger.mapping(record['groups']).items()}
    for pgid in live_groups(set(groups)):
        owner = process_start(pgid)
        if not isinstance(owner, (Started, Zombie)) or owner.start != groups[pgid]:
            raise GateRefused(f'Cannot prove ownership of Mac gate group {pgid}')
        # The leader can exit and the group ID can be reused after the start check.
        with suppress(ProcessLookupError):
            process_group.send_process_group(pgid, signal.SIGKILL)
    while live_groups(set(groups)):
        if time.monotonic() >= deadline:
            raise GateRefused('The Mac gate job groups did not exit')
        time.sleep(0.05)
    folders = record['folders']
    if not isinstance(folders, list):
        raise GateRefused('The Mac gate journal has no folder list')
    for name in cast(list[str], folders):
        path = Path(name)
        with suppress(FileNotFoundError):
            if not build_folders.remove_build_folder(path, lambda: shutil.rmtree(path), wait=False):
                raise GateRefused(f'The Mac kept its gate folder {path}')


def checkout_task(folder: Path) -> str:
    return 'gate:' + attempt_name(folder)


def read_checkout(folder: Path) -> Path:
    row = cloud_gate.read(folder / 'checkout.json')
    path = Path(ledger.text(row, 'path'))
    if not path.is_absolute():
        raise GateRefused('The Mac checkout path is not absolute')
    return path


def stop_checkout(folder: Path) -> None:
    root = (Path.home() / REMOTE_ROOT).resolve()
    if folder.resolve() != root / attempt_name(folder):
        raise GateRefused('The folder is outside the Mac gate state directory')
    try:
        lock = (folder / 'cleanup.lock').open('a')
    except FileNotFoundError:
        return
    with lock:
        grace = conf.read('mac_gate').number('terminate_grace_seconds')
        deadline = time.monotonic() + admission_records.read_memory_config().program_stop_seconds + grace
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GateRefused('The Mac gate cleanup lock is held') from None
                time.sleep(0.05)
        if not folder.exists():
            return
        stop_guest(folder, deadline)
        paths: set[Path] = {read_checkout(folder)} if (folder / 'checkout.json').exists() else set()
        ended = scratch.end(checkout_task(folder))
        if ended.kept:
            raise GateRefused(f'The Mac kept {ended.kept[0].path}: {ended.kept[0].reason}')
        for path in paths.union(ended):
            copies.unregister_remote('localhost', str(path))
        shutil.rmtree(folder)


def clone_checkout(folder: Path, owner: str, repository: str, session: str) -> Path:
    """Create and register a task-owned clone with the installed copies command."""
    scripts = Path.home() / '.claude/scripts'
    with (folder / 'cleanup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Leave the lock with copies if the SSH command ends during clone setup.
        output = subprocess.check_output([
            str(scripts / 'lib/py'), str(scripts / 'copies/copies.py'), 'clone', str(scripts.parent),
            '--task', checkout_task(folder), '--name', 'mac-gate', '--owner', owner,
            '--repository', repository, '--session', session,
        ], text=True, pass_fds=(lock.fileno(),))
        path = Path(output.strip().splitlines()[-1]) if output.strip() else Path()
        if not path.is_absolute():
            raise GateRefused('Copies did not return a Mac checkout')
        cloud_gate.save(folder / 'checkout.json', {'path': str(path)})

        def mark_disposable(record: copy_record.Registered | copy_record.NotRegistered |
                            copy_record.UnreadableRecord) -> dict[str, object]:
            if not isinstance(record, copy_record.Registered) or record.task != checkout_task(folder):
                raise GateRefused('The Mac checkout belongs to another task')
            return {**record.fields, 'throwaway': True}

        _ = scratch.update_record(path, mark_disposable)
        return path


def checkout_bundle(ssh: list[str], checkout: str, repo: Path, folder: Path,
                    request: dict[str, object]) -> list[Path]:
    """Send both requested tips if the Mac clone lacks either one."""
    revisions = (ledger.text(request, 'commit'), ledger.text(request, 'origin_main'))
    git = shlex.join(['git', '-C', checkout])
    probes = [f'if {git} cat-file -e {shlex.quote(revision + "^{commit}")} 2>/dev/null; '
              + 'then printf "present\n"; else printf "absent\n"; fi' for revision in revisions]
    rows = subprocess.check_output([*ssh, f'{git} rev-parse --verify HEAD && ' + ' && '.join(probes)],
                                   text=True).splitlines()
    if (len(rows) != 3 or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', rows[0]) is None
            or any(row not in ('present', 'absent') for row in rows[1:])):
        raise GateRefused('The Mac checkout did not report its source commits')
    if 'absent' not in rows[1:]:
        return []
    head = rows[0]
    exclude = head if gate.git(repo, 'cat-file', '-e', head + '^{commit}').returncode == 0 else ''
    bundle = folder / 'source.bundle'
    cloud_gate.create_source_bundle(repo, bundle, revisions, exclude=exclude)
    return [bundle]


def lifecycle(phase: str, argv_file: Path, folder: Path, full: str) -> int:
    remote = f'{REMOTE_ROOT}/{attempt_name(folder)}'
    root = '"$HOME"/' + shlex.quote(remote)
    runner = f'/usr/bin/env PYTHONPATH="$HOME/.claude" "$HOME/.claude/scripts/lib/py" {root}/mac_gate.py'
    host = os.environ['MAC_QUEUE_HOST']
    ssh = ['ssh', '-n', '-o', 'BatchMode=yes', host]
    checkout_file = argv_file.with_suffix('.json.checkout')
    cleanup = (f'if [ -f {root}/mac_gate.py ]; then {runner} stop {root}; '
               f'else rm -rf -- {root}; fi')
    if phase == 'cleanup':
        result = subprocess.run([*ssh, cleanup + ' && printf "mac_gate_cleaned\n"'],
                                capture_output=True, text=True, check=False)
        if result.returncode or result.stdout.strip() != 'mac_gate_cleaned':
            detail = ' '.join(result.stderr.split()) or 'The Mac did not confirm checkout cleanup'
            print(f'{CLEANUP_ERROR_PREFIX}{folder.name}:{detail}', flush=True)
            return 1
        if checkout_file.exists():
            copies.unregister_remote(host, checkout_file.read_text())
        return 0

    request = cloud_gate.read(folder / 'request.json')
    local_repo = Path((folder / 'local-repo').read_text())
    owner = copies.source_owner(local_repo)
    repository = copies.repository_name(local_repo)
    session = os.environ.get('CODEX_THREAD_ID') or os.environ.get('CLAUDE_CODE_SESSION_ID') or attempt_name(folder)
    files = [folder / 'request.json', folder / 'module_seconds.json']
    parent = root.rsplit('/', 1)[0]
    created = subprocess.check_output(
        [*ssh, f'umask 077 && mkdir -p -- {parent} && mkdir -- {root} && printf "created\n"'], text=True)
    if created.strip() != 'created':
        raise GateRefused('Could not create the Mac gate state folder')
    _ = subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', str(Path(__file__).resolve()),
                        f'{host}:{remote}/mac_gate.py.new'], check=True)
    _ = argv_file.with_suffix('.json.close').write_text(cleanup)
    clone = f'mv -- {root}/mac_gate.py.new {root}/mac_gate.py && {runner} clone {root} '
    response = subprocess.check_output([*ssh, clone + shlex.join([owner, repository, session])], text=True)
    checkout = response.strip().removeprefix('mac_gate_checkout=')
    if not response.startswith('mac_gate_checkout=') or not Path(checkout).is_absolute() or '\n' in checkout:
        raise GateRefused('The Mac did not confirm the gate checkout')
    write_text_atomic(checkout_file, checkout)
    mirror = copies.RemoteMirror(owner, copies.SelectedRepository(repository), host, checkout, session)
    _ = argv_file.with_suffix('.json.lease').write_text(str(copies.remote_lease_path(mirror)))
    copies.register_remote(mirror)
    files.extend(checkout_bundle(ssh, checkout, local_repo, folder, request))
    _ = subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', *map(str, files), f'{host}:{remote}/'], check=True)
    _ = argv_file.write_text(json.dumps(['/bin/zsh', '-c', f'exec {runner} stage {root} {full}']))
    return 0


def stage_checkout(folder: Path, full: str) -> None:
    """Set the tested commit and base in the task-owned checkout."""
    request = cloud_gate.read(folder / 'request.json')
    commit, base = (ledger.text(request, key) for key in ('commit', 'origin_main'))
    repo = read_checkout(folder)
    with record_guest(folder, repo):
        missing = [revision for revision in dict.fromkeys((commit, base))
                   if gate.git(repo, 'cat-file', '-e', revision + '^{commit}').returncode]
        if missing:
            _ = gate.checked_git(repo, 'fetch', '-q', str(folder / 'source.bundle'), *missing)
        _ = gate.checked_git(repo, 'remote', 'set-url', 'origin', ledger.text(request, 'origin'))
        _ = gate.checked_git(repo, 'update-ref', 'refs/remotes/origin/main', base)
        _ = gate.checked_git(repo, 'checkout', '-q', '--detach', commit)
        shim = str(repo / 'scripts/lib/py')
        os.execv(shim, [shim, str(repo / 'scripts/mac_test/mac_gate.py'), 'guest', str(folder), full])


class MacGateRecovery(gate.GateRecovery):
    @override
    def add_group(self, pid: int) -> None:
        super().add_group(pid)
        if pid not in self.groups:
            # Keep evidence of the launch even if its start cannot be read.
            self.groups[pid] = ''
            self.save()


@contextmanager
def record_guest(folder: Path, repo: Path) -> Generator[None]:
    pid = os.getpid()
    owner = process_start(pid)
    if not isinstance(owner, Started):
        raise GateRefused('The Mac gate guest has no readable process start')
    recovery = MacGateRecovery(folder / 'run.json', str(repo.resolve()), pid, owner.start)
    with (folder / 'cleanup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recovery.save()
    recovery_token = gate.ACTIVE_GATE_RECOVERY.set(recovery)
    try:
        yield
    finally:
        gate.ACTIVE_GATE_RECOVERY.reset(recovery_token)


def run_guest(folder: Path, *, full: bool) -> dict[str, object]:
    repo = read_checkout(folder)
    with record_guest(folder, repo):
        return run_guest_modules(folder, repo, full=full)


def run_guest_modules(folder: Path, repo: Path, *, full: bool) -> dict[str, object]:
    request = cloud_gate.read(folder / 'request.json')
    commit, tree = gate.checked_git(repo, 'rev-parse', 'HEAD', 'HEAD^{tree}').splitlines()
    if (commit, tree) != (request['commit'], request['tree']):
        raise GateRefused('The Mac checkout differs from the requested source')
    timings = gate.read_measures(folder / 'module_seconds.json')
    with redirect_stdout(sys.stderr):
        run = gate.run_suites(repo, full=full, mode=gate.SuiteMode(ledger.text(request, 'mode')),
                              machine=gate.WholeMachine(timings),
                              module_time_limit_s=float(cast(float, request['module_time_limit_s'])),
                              time_limit=gate_record.NoGateTimeLimit('the Mac queue call has a time limit'),
                              selected=gate.SelectedModules(tuple(cast(list[str], request['modules']))))
    return {'attempt': request['attempt'], 'commit': commit, 'tree': tree, 'gate': gate_box.encode_run(run)}


@dataclass
class MacGateDiagnostics:
    output: bytearray = field(default_factory=bytearray)
    queue_failures: list[str] = field(default_factory=list)
    gate_failures: list[str] = field(default_factory=list)
    queue_status: int = 0

    def read_failure_record(self, path: Path) -> None:
        from scripts.mac_test.mac_queue_run import QueueFailureStep, read_queue_failures

        for failure in read_queue_failures(path):
            if failure.step is QueueFailureStep.GUEST:
                reason = f'The Mac gate guest ended with status {failure.status}'
            elif failure.step is QueueFailureStep.CLEANUP:
                reason = 'The Mac gate cleanup failed'
            else:
                label = failure.step.value.replace('-', ' ')
                reason = f'The Mac queue {label} failed with status {failure.status}'
            self.queue_failures.append(reason)

    def format_failure(self) -> str:
        reasons = self.queue_failures + self.gate_failures
        if not self.queue_failures and self.queue_status:
            reasons.append(f'The Mac queue call ended with status {self.queue_status}')
        if self.output:
            reasons.append(self.output.decode('utf-8', errors='surrogateescape'))
        return '\n'.join(reasons)


def report_interrupted_gate(report: str) -> None:
    if not report.endswith('\n'):
        report += '\n'
    with suppress(OSError, UnicodeError):
        if isinstance(sys.stderr, io.TextIOWrapper):
            _ = sys.stderr.flush()
            _ = sys.stderr.buffer.write(report.encode('utf-8', errors='surrogateescape'))
            _ = sys.stderr.buffer.flush()
        else:
            _ = sys.stderr.write(report)


def run_remote(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
               selected: gate.AllModules | gate.TreeSelection,
               cancellation: MacGateCancellation | UseCallerProcessLifetime,
               ) -> MacGateResult | str:
    """Wait for a Mac placement in queue order and run the assigned modules."""
    diagnostics = MacGateDiagnostics()
    outcome: MacGateResult | str = ''
    deferred_exit: BaseException | None = None
    attempt_error: BaseException | None = None
    try:
        with ExitStack() as cleanup:
            try:
                outcome = run_remote_attempt(repo, cleanup, diagnostics, mode=mode, full=full,
                                             module_time_limit_s=module_time_limit_s,
                                             selected=selected, cancellation=cancellation)
                if isinstance(outcome, str) and outcome:
                    diagnostics.gate_failures.append(outcome)
            except BaseException as error:
                attempt_error = error
                if not isinstance(error, Exception):
                    deferred_exit = error
                detail = str(error) or type(error).__name__
                diagnostics.gate_failures.append(f'The Mac gate could not finish: {detail}')
                raise
    except BaseException as error:
        if error is not attempt_error:
            if deferred_exit is None and not isinstance(error, Exception):
                deferred_exit = error
            detail = str(error) or type(error).__name__
            diagnostics.gate_failures.append(f'The Mac gate could not finish local cleanup: {detail}')
    if (isinstance(outcome, MacGateResult) and not diagnostics.queue_failures
            and not diagnostics.gate_failures):
        return outcome
    report = diagnostics.format_failure()
    if deferred_exit is not None:
        report_interrupted_gate(report)
        raise deferred_exit
    return report


def collect_queue_output(process: subprocess.Popen[bytes], output: bytearray, deadline: float) -> int:
    """Read queue output and wait for exit within the same time limit."""
    assert process.stdout is not None
    descriptor = process.stdout.fileno()
    with selectors.DefaultSelector() as ready:
        _ = ready.register(descriptor, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not ready.select(remaining):
                raise subprocess.TimeoutExpired(process.args, MAC_TIME_LIMIT_S)
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            output.extend(chunk)
    return process.wait(timeout=max(0, deadline - time.monotonic()))


def run_remote_attempt(repo: Path, cleanup: ExitStack, diagnostics: MacGateDiagnostics, *,
                       mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
                       selected: gate.AllModules | gate.TreeSelection,
                       cancellation: MacGateCancellation | UseCallerProcessLifetime) -> MacGateResult | str:
    from scripts.production.gate_routes import MacGateCancellation

    repo = repo.resolve()
    if isinstance(cancellation, MacGateCancellation):
        caller = os.dup(cancellation.caller_fd)
    else:
        caller, lifetime = os.pipe()
        _ = cleanup.callback(os.close, lifetime)
    _ = cleanup.callback(os.close, caller)
    cancelled = select.poll()
    cancelled.register(caller, select.POLLIN)
    if cancelled.poll(0):
        return 'The Mac gate was cancelled'
    folder = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix='mac-gate-')))
    failure_file = folder / 'queue-failure'
    # Read the record before the stack removes the temporary folder.
    _ = cleanup.callback(diagnostics.read_failure_record, failure_file)
    request = cloud_gate.prepare_request(repo, folder, mode, module_time_limit_s, selected=selected)
    cloud_gate.save(folder / 'request.json', request)
    _ = (folder / 'local-repo').write_text(str(repo))
    if not request['modules']:
        return 'No modules selected for the Mac'
    timings = gate.read_measures(gate.gate_state_dir() / 'module_seconds.json')
    _ = (folder / 'module_seconds.json').write_text(json.dumps(timings))
    command = [*QUEUE_COMMAND, '--what', 'gate tests', '--kind', 'run', '--package', '',
               '--worktree', str(repo), '--owned-close', '--failure-file', str(failure_file),
               '--lifecycle-script', str(Path(__file__).resolve()),
               '--', str(folder), str(int(full))]
    command[2:2] = ['--caller-fd', str(caller)]
    if cancelled.poll(0):
        return 'The Mac gate was cancelled'
    process = cleanup.enter_context(subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                      start_new_session=True, pass_fds=(caller,)))
    try:
        status = collect_queue_output(process, diagnostics.output, time.monotonic() + MAC_TIME_LIMIT_S)
    except BaseException as error:
        timed_out = isinstance(error, subprocess.TimeoutExpired)
        if timed_out:
            diagnostics.gate_failures.append(f'The Mac queue call exceeded {MAC_TIME_LIMIT_S:g} seconds')
        try:
            process_group.send_process_group(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as stop_error:
            diagnostics.gate_failures.append(f'The Mac gate could not stop the queue call: {stop_error}')
        if timed_out:
            return ''
        raise
    if cancelled.poll(0):
        return 'The Mac gate was cancelled'
    if status:
        diagnostics.queue_status = status
        return ''
    text = diagnostics.output.decode('utf-8', errors='surrogateescape')
    results = [line.removeprefix(RESULT_PREFIX) for line in text.splitlines() if line.startswith(RESULT_PREFIX)]
    if len(results) != 1:
        return 'The Mac returned no unique gate result'
    row = ledger.mapping(cast(object, json.loads(results[0])))
    run = cloud_gate.decode_requested_run(row, request)
    cloud_gate.validate_run_modules(run, request)
    return MacGateResult(row, run)


def route(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
          selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES,
          cancellation: MacGateCancellation | UseCallerProcessLifetime = USE_CALLER_PROCESS_LIFETIME) -> gate.GateRun | str:
    """Return the observed module results or a reason to run them on natedev."""
    outcome = run_remote(repo, mode=mode, full=full, module_time_limit_s=module_time_limit_s,
                         selected=selected, cancellation=cancellation)
    return outcome if isinstance(outcome, str) else outcome.run


@dataclass(frozen=True)
class ModuleCounts:
    tests: int
    skips: int


def natedev_counts(path: Path, tree: str) -> dict[str, ModuleCounts]:
    """Read the last measured attempt per module in a recorded full run."""
    counts: dict[str, ModuleCounts] = {}
    found = False
    for row in store.locked_rows(path):
        if (row.get('kind'), row.get('host'), row.get('tree'), row.get('selection'),
                row.get('dirty'), row.get('aborted')) != ('gate_run', 'natedev', tree, 'full', False, False):
            continue
        found = True
        counts.clear()
        attempts: dict[str, int] = {}
        for module in ledger.rows(row, 'modules'):
            name = f"scripts/{ledger.text(module, 'suite')}/{ledger.text(module, 'module')}"
            attempt = ledger.instant(module, 'attempt')
            if attempt <= attempts.get(name, 0):
                continue
            attempts[name] = attempt
            _ = counts.pop(name, None)
            if type(module.get('status')) is int and module['status'] == 0 and module.get('times_missing') == '':
                counts[name] = ModuleCounts(ledger.instant(module, 'tests_run'), ledger.instant(module, 'skipped'))
    if not found:
        raise GateRefused('No complete natedev gate record has the Mac result tree')
    return counts


def write_modules(repo: Path, mac_result: Path, natedev_record: Path) -> None:
    row = cloud_gate.read(mac_result)
    tree = ledger.text(row, 'tree')
    if cloud_gate.working_tree(repo)[0] != tree:
        raise GateRefused('The checkout tree differs from the Mac result')
    baseline = natedev_counts(natedev_record, tree)
    observed = set(baseline)
    passed: set[str] = set()
    for job in gate_box.decode_run(row['gate']).jobs:
        if job.kind is not gate.GateJobKind.TEST_MODULE:
            continue
        name = f'scripts/{job.suite}/{job.module}'
        observed.add(name)
        if (job.returncode == 0 and not job.killed_for
                and isinstance(job.times, dict)
                and baseline.get(name) == ModuleCounts(ledger.instant(ledger.mapping(job.times), 'tests_run'),
                                                       ledger.instant(ledger.mapping(job.times), 'skipped'))):
            passed.add(name)
    write_text_atomic(repo / MODULE_LIST, MODULE_LIST_HEADER + ''.join(
        name + '\n' for name in sorted(observed - passed)))


@dataclass
class Arguments(argparse.Namespace):
    action: str = ''
    folder: Path = Path()
    full: str = ''
    argv_file: Path = Path()
    repo: Path = Path()
    output: Path = Path()
    mac_result: Path = Path()
    natedev_record: Path = Path()
    owner: str = ''
    repository: str = ''
    session: str = ''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    manual = commands.add_parser('run', help='run all modules on the Mac and save the result')
    _ = manual.add_argument('repo', type=Path)
    _ = manual.add_argument('output', type=Path)
    writer = commands.add_parser('write-list', help='write exclusions from a Mac result and a natedev gate log')
    _ = writer.add_argument('repo', type=Path)
    _ = writer.add_argument('mac_result', type=Path)
    _ = writer.add_argument('natedev_record', type=Path)
    for phase in ('prepare', 'cleanup'):
        callback = commands.add_parser(phase)
        _ = callback.add_argument('argv_file', type=Path)
        _ = callback.add_argument('folder', type=Path)
        _ = callback.add_argument('full', choices=('0', '1'))
    clone = commands.add_parser('clone')
    _ = clone.add_argument('folder', type=Path)
    _ = clone.add_argument('owner')
    _ = clone.add_argument('repository')
    _ = clone.add_argument('session')
    for phase in ('stage', 'guest'):
        guest = commands.add_parser(phase)
        _ = guest.add_argument('folder', type=Path)
        _ = guest.add_argument('full', choices=('0', '1'))
    _ = commands.add_parser('stop').add_argument('folder', type=Path)
    args = parser.parse_args(namespace=Arguments())
    try:
        if args.action in ('prepare', 'cleanup'):
            return lifecycle(args.action, args.argv_file, args.folder, args.full)
        if args.action == 'clone':
            path = clone_checkout(args.folder, args.owner, args.repository, args.session)
            print(f'mac_gate_checkout={path}', flush=True)
        elif args.action == 'stage':
            stage_checkout(args.folder, args.full)
        elif args.action == 'guest':
            print(RESULT_PREFIX + json.dumps(run_guest(args.folder, full=args.full == '1')), flush=True)
        elif args.action == 'stop':
            if args.folder.exists():
                stop_checkout(args.folder)
        elif args.action == 'write-list':
            write_modules(args.repo.resolve(), args.mac_result, args.natedev_record)
        else:
            result = run_remote(args.repo.resolve(), mode=gate.SuiteMode.MERGE_GATE, full=True,
                                module_time_limit_s=gate.DEFAULT_MODULE_TIME_LIMIT_S, selected=gate.ALL_MODULES,
                                cancellation=USE_CALLER_PROCESS_LIFETIME)
            if isinstance(result, str):
                raise GateRefused(result)
            cloud_gate.save(args.output, result.row)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, GateRefused, GraphError, copies.CopyError, scratch.ScratchError) as error:
        print(f'Mac gate: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

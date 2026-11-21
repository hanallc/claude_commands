#!/usr/bin/env python3
"""Run gate modules through the Mac queue and compare their counts with natedev."""
from __future__ import annotations

import argparse
from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager, redirect_stdout, suppress
from dataclasses import dataclass
from enum import StrEnum
import fcntl
import json
import os
import re
from pathlib import Path
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import cast, override

if __package__ in ('', None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.aws_target import cloud_gate, gate_box, ledger
from scripts.build_queue import admission_records
from scripts.buildlog import store
from scripts.copies import copies
from scripts.lib import build_folders, conf, copy_record, process_group, scratch
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_start import Started, Unreadable, Zombie, process_start
from scripts.production import gate
from scripts.production.affected_modules import GraphError
from scripts.production.gate_modules import GateRefused, discover_test_modules
from scripts.test_times import gate_record

MODULE_LIST = Path('config/mac_gate_modules.txt')
MODULE_LIST_HEADER = '# List test modules that must run on natedev because the Mac cannot run them.\n'
REMOTE_ROOT = '.local/state/mac-gate'
CHECKOUT_NAME = 'mac-gate'
ATTEMPT_PREFIX = CHECKOUT_NAME + '-'
ATTEMPT_PATTERN = rf'{ATTEMPT_PREFIX}[A-Za-z0-9_]+'
CHECKOUT_TASK_PREFIX = 'gate:'
RESULT_PREFIX = 'mac_gate_result='
CLEANUP_ERROR_PREFIX = 'mac_gate_cleanup_error='
QUEUE_COMMAND = (sys.executable, str(Path(__file__).with_name('mac_queue_run.py')))
MAC_TIME_LIMIT_S = 3600


class CheckoutCleanup(StrEnum):
    REMOVED = 'mac_gate_cleaned'
    RETAINED = 'mac_gate_retained'


@dataclass(frozen=True)
class MacGateResult:
    row: dict[str, object]
    run: gate.GateRun


@dataclass(frozen=True)
class UseCallerProcessLifetime:
    """The Mac route watches its calling process for exit."""


USE_CALLER_PROCESS_LIFETIME = UseCallerProcessLifetime()


def read_modules(repo: Path) -> gate.MacGateExclusions | gate.MacGateExcludeFileMissing:
    """Return excluded test paths in the tested checkout."""
    try:
        entries = {line for row in (repo / MODULE_LIST).read_text().splitlines()
                   if (line := row.partition('#')[0].strip())}
    except FileNotFoundError:
        return gate.MacGateExcludeFileMissing()
    if not entries:
        return gate.MacGateExclusions(())
    tracked_paths = set(gate.checked_git(repo, 'ls-files', '-z', '--cached', '--others',
                                        '--exclude-standard', '--', 'scripts').split('\0'))
    excluded = entries & tracked_paths
    return gate.MacGateExclusions(tuple(name for _, path in discover_test_modules(repo)
                                        if (name := path.relative_to(repo).as_posix()) in excluded))


def attempt_name(folder: Path) -> str:
    attempt = folder.name
    if re.fullmatch(ATTEMPT_PATTERN, attempt) is None:
        raise GateRefused('Invalid Mac gate attempt')
    return attempt


def is_mac_gate_checkout(mirror: copies.RemoteMirror) -> bool:
    # A record without a task goes through the manual removal list.
    return (mirror.task.startswith(CHECKOUT_TASK_PREFIX)
            and re.fullmatch(ATTEMPT_PATTERN, mirror.task.removeprefix(CHECKOUT_TASK_PREFIX)) is not None)


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


def stop_inline_checkout(repo: Path) -> None:
    """Stop users of an inline checkout from before the guest journal."""
    if repo.is_symlink():
        raise GateRefused('The Mac gate checkout is a symbolic link')
    if not repo.is_dir():
        return
    from scripts.lint import folder_audit

    def users() -> set[int]:
        uses = folder_audit.process_uses([repo])
        if isinstance(uses, folder_audit.ProcessUsesUnavailable):
            raise GateRefused('Cannot check for test processes on the Mac')
        return {pid for path, pid, _ in uses.paths if pid > 1 and pid != os.getpid()
                and Path(path).is_relative_to(repo)}

    processes = {pid: process_start(pid) for pid in users()}
    if not processes:
        return
    for pid in users() & processes.keys():
        identity = processes[pid]
        if isinstance(identity, Started) and process_start(pid) == identity:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    if users():
        raise GateRefused('Test processes still use the Mac checkout')


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
    return CHECKOUT_TASK_PREFIX + attempt_name(folder)


def read_checkout(folder: Path) -> Path:
    row = cloud_gate.read(folder / 'checkout.json')
    path = Path(ledger.text(row, 'path'))
    if not path.is_absolute():
        raise GateRefused('The Mac checkout path is not absolute')
    return path


def saved_checkout_status(checkout: Path | None) -> CheckoutCleanup:
    if checkout is not None:
        try:
            _ = checkout.lstat()
        except FileNotFoundError:
            pass
        else:
            return CheckoutCleanup.RETAINED
    return CheckoutCleanup.REMOVED


def stop_checkout(folder: Path, saved_checkout: Path | None = None) -> CheckoutCleanup:
    root = (Path.home() / REMOTE_ROOT).resolve()
    if folder.resolve() != root / attempt_name(folder):
        raise GateRefused('The folder is outside the Mac gate state directory')
    if saved_checkout is not None and not saved_checkout.is_absolute():
        raise GateRefused('The saved Mac checkout path is not absolute')
    try:
        lock = (folder / 'cleanup.lock').open('a')
    except FileNotFoundError:
        return saved_checkout_status(saved_checkout)
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
            return saved_checkout_status(saved_checkout)
        stop_inline_checkout(folder.resolve() / 'repo')
        checkout_file = folder / 'checkout.json'
        receipt = folder / 'task-ended.json'
        cleanup_paths: dict[str, object]
        try:
            cleanup_paths = cloud_gate.read(receipt)
        except FileNotFoundError:
            stop_guest(folder, deadline)
            # Until Mac promotion, scratch.end can return removed paths and a separate kept list.
            ended = cast(Sequence[Path | scratch.KeptFolder], scratch.end(checkout_task(folder)))
            legacy_kept = cast(Sequence[scratch.KeptFolder], getattr(ended, 'kept', ()))
            for entry in legacy_kept:
                print(f'kept for a manual removal list: {entry.path} (scratch, {entry.reason})', file=sys.stderr)
            cleanup_paths = {str(entry.path): entry.reason for entry in (*ended, *legacy_kept)
                             if isinstance(entry, scratch.KeptFolder)}
            removed = {entry for entry in ended if isinstance(entry, Path)}
            checkout = read_checkout(folder) if removed and checkout_file.exists() else None
            for path in sorted(removed):
                if not path.is_absolute():
                    raise GateRefused('The Mac removed folder path is not absolute')
                if str(path) in cleanup_paths:
                    continue
                try:
                    _ = path.lstat()
                except FileNotFoundError:
                    copies.unregister_remote('localhost', str(path))
                    if path == checkout:
                        checkout_file.unlink()
                else:
                    cleanup_paths[str(path)] = 'the folder is still present'
            cloud_gate.save(receipt, cleanup_paths)
        paths: set[Path] = {read_checkout(folder)} if checkout_file.exists() else set()
        paths.update(Path(name) for name in cleanup_paths)
        outcome = CheckoutCleanup.REMOVED
        for path in sorted(paths):
            if not path.is_absolute():
                raise GateRefused('The Mac retained folder path is not absolute')
            try:
                _ = path.lstat()
            except FileNotFoundError:
                copies.unregister_remote('localhost', str(path))
            else:
                outcome = CheckoutCleanup.RETAINED
        if outcome is CheckoutCleanup.REMOVED:
            shutil.rmtree(folder)
            return saved_checkout_status(saved_checkout)
        return outcome


def clone_checkout(folder: Path, owner: str, repository: str, session: str) -> Path:
    """Create and register a task-owned clone with the installed copies command."""
    scripts = Path.home() / '.claude/scripts'
    with (folder / 'cleanup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Leave the lock with copies if the SSH command ends during clone setup.
        output = subprocess.check_output([
            str(scripts / 'lib/py'), str(scripts / 'copies/copies.py'), 'clone', str(scripts.parent),
            '--task', checkout_task(folder), '--name', CHECKOUT_NAME, '--owner', owner,
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


def checkout_cleanup_command(root: str, runner: str, checkout: str) -> str:
    fallback = f'printf "{CheckoutCleanup.RETAINED}\\n"'
    if Path(checkout).is_absolute():
        probe = copies.path_missing_command(checkout)
        fallback = f'if {probe}; then printf "{CheckoutCleanup.REMOVED}\\n"; else {fallback}; fi'
    return (f'if [ -f {root}/mac_gate.py ]; then {runner} stop {root}; '
            f'else rm -rf -- {root} && {fallback}; fi')


def lifecycle(phase: str, argv_file: Path, folder: Path, full: str) -> int:
    remote = f'{REMOTE_ROOT}/{attempt_name(folder)}'
    root = '"$HOME"/' + shlex.quote(remote)
    python = '/usr/bin/env PYTHONPATH="$HOME/.claude" "$HOME/.claude/scripts/lib/py"'
    runner = f'{python} {root}/mac_gate.py'
    host = os.environ['MAC_QUEUE_HOST']
    ssh = ['ssh', '-n', '-o', 'BatchMode=yes', host]
    checkout_file = argv_file.with_suffix('.json.checkout')
    if phase in ('cleanup', 'cleanup-ended'):
        try:
            checkout = checkout_file.read_text()
        except FileNotFoundError:
            checkout = ''
        if phase == 'cleanup-ended':
            from scripts.mac_test.mac_queue_run import MAC_GATE_SCRIPT

            cleanup = f'{python} "$HOME/.claude"/{MAC_GATE_SCRIPT} stop {root}'
            if Path(checkout).is_absolute():
                cleanup += ' ' + shlex.join(['--checkout', checkout])
        else:
            cleanup = checkout_cleanup_command(root, runner, checkout)
        result = subprocess.run([*ssh, cleanup], capture_output=True, text=True, check=False)
        outcome = result.stdout.strip()
        if result.returncode or outcome not in (CheckoutCleanup.REMOVED, CheckoutCleanup.RETAINED):
            detail = ' '.join(result.stderr.split()) or 'The Mac did not confirm checkout cleanup'
            print(f'{CLEANUP_ERROR_PREFIX}{folder.name}:{detail}', flush=True)
            return 1
        if result.stderr:
            print(result.stderr, end='', file=sys.stderr)
        if outcome == CheckoutCleanup.REMOVED and Path(checkout).is_absolute():
            copies.unregister_remote(host, checkout)
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
    clone = f'mv -- {root}/mac_gate.py.new {root}/mac_gate.py && {runner} clone {root} '
    response = subprocess.check_output([*ssh, clone + shlex.join([owner, repository, session])], text=True)
    checkout = response.strip().removeprefix('mac_gate_checkout=')
    if not response.startswith('mac_gate_checkout=') or not Path(checkout).is_absolute() or '\n' in checkout:
        raise GateRefused('The Mac did not confirm the gate checkout')
    write_text_atomic(checkout_file, checkout)
    _ = argv_file.with_suffix('.json.close').write_text(checkout_cleanup_command(root, runner, checkout))
    mirror = copies.RemoteMirror(owner, copies.SelectedRepository(repository), host, checkout, session,
                                 task=checkout_task(folder))
    _ = argv_file.with_suffix('.json.lease').write_text(str(copies.remote_lease_path(mirror)))
    copies.register_remote(mirror)
    copies.reconcile_remote_mirrors(host, is_mac_gate_checkout)
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
        if (folder / 'task-ended.json').exists():
            raise GateRefused('The Mac gate checkout task has ended')
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


def run_remote(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
               selected: gate.AllModules | gate.TreeSelection,
               cancellation: gate.MacGateCancellation | UseCallerProcessLifetime,
               ) -> MacGateResult | str:
    """Wait for a Mac placement in queue order and run the assigned modules."""
    try:
        repo = repo.resolve()
        with ExitStack() as cleanup:
            if isinstance(cancellation, gate.MacGateCancellation):
                caller = os.dup(cancellation.caller_fd)
            else:
                caller, lifetime = os.pipe()
                _ = cleanup.callback(os.close, lifetime)
            _ = cleanup.callback(os.close, caller)
            cancelled = select.poll()
            cancelled.register(caller, select.POLLIN)
            if cancelled.poll(0):
                return 'The Mac gate was cancelled'
            folder = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix=ATTEMPT_PREFIX)))
            request = cloud_gate.prepare_request(repo, folder, mode, module_time_limit_s, selected=selected)
            cloud_gate.save(folder / 'request.json', request)
            _ = (folder / 'local-repo').write_text(str(repo))
            if not request['modules']:
                return 'No modules selected for the Mac'
            timings = gate.read_measures(gate.gate_state_dir() / 'module_seconds.json')
            _ = (folder / 'module_seconds.json').write_text(json.dumps(timings))
            command = [*QUEUE_COMMAND, '--what', 'gate tests', '--kind', 'run', '--package', '',
                       '--worktree', str(repo), '--owned-close',
                       '--lifecycle-script', str(Path(__file__).resolve()),
                       '--', str(folder), str(int(full))]
            command[2:2] = ['--caller-fd', str(caller)]
            if cancelled.poll(0):
                return 'The Mac gate was cancelled'
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding='utf-8', errors='replace', start_new_session=True,
                                       pass_fds=(caller,))
            with process:
                try:
                    output, _ = process.communicate(timeout=MAC_TIME_LIMIT_S)
                except BaseException:
                    with suppress(ProcessLookupError):
                        process_group.send_process_group(process.pid, signal.SIGKILL)
                    raise
            lines = output.splitlines()
            was_cancelled = bool(cancelled.poll(0))
            status = process.returncode
            assert status is not None
            if status or was_cancelled:
                return queue_failure(lines, folder, status, cancelled=was_cancelled)
            results = [line.removeprefix(RESULT_PREFIX) for line in lines if line.startswith(RESULT_PREFIX)]
            if len(results) != 1:
                return 'The Mac returned no unique gate result'
            row = ledger.mapping(cast(object, json.loads(results[0])))
            run = cloud_gate.decode_requested_run(row, request)
            cloud_gate.validate_run_modules(run, request)
            return MacGateResult(row, run)
    except subprocess.TimeoutExpired:
        return f'The Mac queue call exceeded {MAC_TIME_LIMIT_S:g} seconds'
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, GateRefused, GraphError, copies.CopyError, scratch.ScratchError) as error:
        return f'The Mac gate could not finish: {error}'


def queue_failure(lines: list[str], folder: Path, status: int, *, cancelled: bool) -> str:
    prefix = f'{CLEANUP_ERROR_PREFIX}{folder.name}:'
    cleanup = next((line.removeprefix(prefix).strip() for line in reversed(lines) if line.startswith(prefix)), '')
    reason = cleanup or ('The Mac gate was cancelled' if cancelled else f'The Mac queue call ended with status {status}')
    detail = next((line.strip().removeprefix(prefix).strip() for line in reversed(lines) if line.strip()), '')
    return f'{reason}: {detail}' if detail and detail != reason and not detail.startswith(RESULT_PREFIX) else reason


def route(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
          selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES,
          cancellation: gate.MacGateCancellation | UseCallerProcessLifetime = USE_CALLER_PROCESS_LIFETIME) -> gate.GateRun | str:
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
    checkout: Path | None = None
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
    for phase in ('prepare', 'cleanup', 'cleanup-ended'):
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
    stop = commands.add_parser('stop')
    _ = stop.add_argument('folder', type=Path)
    _ = stop.add_argument('--checkout', type=Path)
    args = parser.parse_args(namespace=Arguments())
    try:
        if args.action in ('prepare', 'cleanup', 'cleanup-ended'):
            return lifecycle(args.action, args.argv_file, args.folder, args.full)
        if args.action == 'clone':
            path = clone_checkout(args.folder, args.owner, args.repository, args.session)
            print(f'mac_gate_checkout={path}', flush=True)
        elif args.action == 'stage':
            stage_checkout(args.folder, args.full)
        elif args.action == 'guest':
            print(RESULT_PREFIX + json.dumps(run_guest(args.folder, full=args.full == '1')), flush=True)
        elif args.action == 'stop':
            print(stop_checkout(args.folder, args.checkout))
        elif args.action == 'write-list':
            write_modules(args.repo.resolve(), args.mac_result, args.natedev_record)
        else:
            result = run_remote(args.repo.resolve(), mode=gate.SuiteMode.GATE, full=True,
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

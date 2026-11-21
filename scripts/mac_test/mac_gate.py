#!/usr/bin/env python3
"""Run gate modules through the Mac queue and compare their counts with natedev."""
from __future__ import annotations

import argparse
from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager, redirect_stdout, suppress
from dataclasses import dataclass, field
from enum import Enum, StrEnum
import fcntl
import json
import os
import re
from pathlib import Path
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from typing import TYPE_CHECKING, cast, override

if __package__ in ('', None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.agents.worker_lifecycle import acquire_flock, operation_deadline
from scripts.aws_target import cloud_gate, gate_box, ledger
from scripts.buildlog import store
from scripts.copies import copies
from scripts.lib import build_folders, conf, copy_record, process_group, scratch
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_start import Started, Unreadable, Zombie, process_start
from scripts.mac_test.claim_state import (
    AssignedGatePaths as AssignedGatePaths,
    QUEUE_RUNS as QUEUE_RUNS,
    assigned_gate_paths as assigned_gate_paths,
    checkout_cleanup_seconds as checkout_cleanup_seconds,
    gate_requests_directory as gate_requests_directory,
    queue_cancel_seconds as queue_cancel_seconds,
    queue_runs_directory as queue_runs_directory,
    CREATION_LEASE_FD as CREATION_LEASE_FD,
    QueueProcessUnsettled as QueueProcessUnsettled,
    capture_cleanup_command as capture_cleanup_command,
    close_process_pipes as close_process_pipes,
    kill_and_wait as kill_and_wait,
    terminate_and_wait as terminate_and_wait,
)
from scripts.production import merge_gate as gate
from scripts.production.affected_modules import GraphError
from scripts.production.gate_modules import GateRefused, discover_test_modules
from scripts.test_times import gate_record

if TYPE_CHECKING:
    from scripts.build_queue.placement import PlacementAssignment
    from scripts.production.gate_routes import (
        MacGateCancellation, MacGateCompleted, MacGateExcludeFileMissing, MacGateExclusions,
        MacGatePrelaunchRefused, MacGateUnresolved,
    )

MODULE_LIST = Path('config/mac_gate_modules.txt')
MODULE_LIST_HEADER = '# List test modules that must run on natedev because the Mac cannot run them.\n'
REMOTE_PREPARATION = '.local/state/mac-gate-preparation'
REMOTE_CREATION_LEASE_FD = 'MAC_GATE_REMOTE_CREATION_LEASE_FD'
REMOTE_SETTLEMENT_DEADLINE = 'MAC_GATE_REMOTE_SETTLEMENT_DEADLINE'
REMOTE_ROOT = '.local/state/mac-gate'
CHECKOUT_NAME = 'mac-gate'
ATTEMPT_PREFIX = CHECKOUT_NAME + '-'
ATTEMPT_PATTERN = rf'{ATTEMPT_PREFIX}[A-Za-z0-9_]+'
CHECKOUT_TASK_PREFIX = 'gate:'
RESULT_PREFIX = 'mac_gate_result='
CLEANUP_ERROR_PREFIX = 'mac_gate_cleanup_error='
ASSIGNED_REFUSAL = 'placement-refusal.json'
ASSIGNED_COMPLETION = 'placement-completed'
QUEUE_COMMAND = (sys.executable, str(Path(__file__).with_name('mac_queue_run.py')))
MAC_TIME_LIMIT_S = 3600


class CheckoutCleanup(StrEnum):
    REMOVED = 'mac_gate_cleaned'
    RETAINED = 'mac_gate_retained'


@dataclass(frozen=True)
class MacGateResult:
    row: dict[str, object]
    run: gate.GateRun


@dataclass
class MacGateResultLines:
    """Collect result lines from the queue output."""

    _payloads: list[str] = field(default_factory=list)

    def record(self, line: str) -> None:
        if line.startswith(RESULT_PREFIX):
            self._payloads.append(line.removeprefix(RESULT_PREFIX))

    def decode(self, request: dict[str, object]) -> MacGateResult:
        if len(self._payloads) != 1:
            raise ValueError('The Mac returned no unique gate result')
        row = ledger.mapping(cast(object, json.loads(self._payloads[0])))
        run = cloud_gate.decode_requested_run(row, request)
        cloud_gate.validate_run_modules(run, request)
        return MacGateResult(row, run)


@dataclass(frozen=True)
class UseCallerProcessLifetime:
    """The Mac route watches its calling process for exit."""


USE_CALLER_PROCESS_LIFETIME = UseCallerProcessLifetime()


@dataclass(frozen=True)
class RequestMacPlacement:
    """The manual Mac gate requests its own placement."""


REQUEST_MAC_PLACEMENT = RequestMacPlacement()


@contextmanager
def gate_preparation_lock(path: Path) -> Generator[None]:
    """Prevent cleanup while the gate writes its request."""
    descriptor = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


@contextmanager
def queue_call(command: list[str], arguments: list[str], caller_fds: list[int], caller_lifetime: ExitStack,
               cancel_seconds: float) -> Generator[tuple[subprocess.Popen[str], socket.socket]]:
    gate_preparation, queue_preparation = socket.socketpair()
    with gate_preparation, queue_preparation:
        queue_fd = queue_preparation.fileno()
        process = subprocess.Popen(
            [*command, '--gate-preparation-fd', str(queue_fd), '--cancel-seconds', str(cancel_seconds), '--', *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', start_new_session=True, pass_fds=(*caller_fds, queue_fd),
        )
        try:
            queue_preparation.close()
            yield process, gate_preparation
        finally:
            gate_preparation.close()
            try:
                if process.returncode is None:
                    caller_lifetime.close()
                    deadline = time.monotonic() + cancel_seconds
                    grace = conf.read('mac_gate').number('terminate_grace_seconds')
                    try:
                        _ = process.communicate(timeout=max(0, cancel_seconds - grace))
                    except subprocess.TimeoutExpired:
                        kill_and_wait(process, deadline, owns_group=True, owns_session=True)
            finally:
                close_process_pipes(process)


class SettledMacRefusal(Exception):
    """The service confirmed refusal and cleanup before launch."""


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


def live_groups(groups: set[int], deadline: float) -> set[int]:
    if not groups:
        return set()
    result = capture_cleanup_command(['ps', '-axo', 'pgid=,stat='], max(0, min(5, deadline - time.monotonic())))
    result.check_returncode()
    rows = (line.split() for line in result.stdout.splitlines())
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
    for pgid in live_groups(set(groups), deadline):
        owner = process_start(pgid)
        if not isinstance(owner, (Started, Zombie)) or owner.start != groups[pgid]:
            raise GateRefused(f'Cannot prove ownership of Mac gate group {pgid}')
        # The leader can exit and the group ID can be reused after the start check.
        with suppress(ProcessLookupError):
            process_group.send_process_group(pgid, signal.SIGKILL)
    while live_groups(set(groups), deadline):
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
    limit = float(os.environ.get(REMOTE_SETTLEMENT_DEADLINE, 'inf'))
    with lock, operation_deadline(deadline=limit, timeout=checkout_cleanup_seconds()) as deadline:
        acquire_flock(lock)
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


def supervise_checkout_cleanup(folder: Path, saved_checkout: Path | None = None) -> CheckoutCleanup:
    """Keep the remote lease with the worker until cleanup exits."""
    if REMOTE_SETTLEMENT_DEADLINE in os.environ or not folder.exists():
        return stop_checkout(folder, saved_checkout)
    lease = os.environ.get(REMOTE_CREATION_LEASE_FD)
    locks = () if lease is None else (int(lease),)
    command = [sys.executable, str(Path(__file__).resolve()), 'remove-checkout', str(folder)]
    if saved_checkout is not None:
        command.extend(('--checkout', str(saved_checkout)))
    result = capture_cleanup_command(command, checkout_cleanup_seconds(), pass_fds=locks, supervise_session=True)
    if result.returncode:
        raise GateRefused(result.stderr.strip() or 'The Mac checkout cleanup remains unresolved')
    if result.stderr:
        print(result.stderr, end='', file=sys.stderr)
    return CheckoutCleanup(result.stdout.strip())


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


def bounded_bundle_git(repository: Path, /, *arguments: str) -> subprocess.CompletedProcess[str]:
    return capture_cleanup_command(['git', *arguments], checkout_cleanup_seconds(), cwd=repository,
                                   pass_fds=copies.COMMAND_LOCKS.get())


def checkout_bundle(ssh: list[str], checkout: str, repo: Path, folder: Path,
                    request: dict[str, object], *,
                    journal: Path | cloud_gate.NoBundleRefJournal = cloud_gate.NO_BUNDLE_REF_JOURNAL) -> list[Path]:
    """Send both requested tips if the Mac clone lacks either one."""
    revisions = (ledger.text(request, 'commit'), ledger.text(request, 'origin_main'))
    git = shlex.join(['git', '-C', checkout])
    probes = [f'if {git} cat-file -e {shlex.quote(revision + "^{commit}")} 2>/dev/null; '
              + 'then printf "present\n"; else printf "absent\n"; fi' for revision in revisions]
    rows = subprocess.check_output([*ssh, f'{git} rev-parse --verify HEAD && ' + ' && '.join(probes)],
                                   text=True, pass_fds=copies.COMMAND_LOCKS.get()).splitlines()
    if (len(rows) != 3 or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', rows[0]) is None
            or any(row not in ('present', 'absent') for row in rows[1:])):
        raise GateRefused('The Mac checkout did not report its source commits')
    if 'absent' not in rows[1:]:
        return []
    head = rows[0]
    exclude = head if bounded_bundle_git(repo, 'cat-file', '-e', head + '^{commit}').returncode == 0 else ''
    bundle = folder / 'source.bundle'
    cloud_gate.create_source_bundle(repo, bundle, revisions, exclude=exclude, journal=journal,
                                    run_git=bounded_bundle_git)
    return [bundle]


@contextmanager
def hold_gate_creation_commands() -> Generator[None]:
    """Give preparation children the same creation lease."""
    lease = os.environ.get(CREATION_LEASE_FD)
    if lease is None:
        yield
        return
    token = copies.COMMAND_LOCKS.set((*copies.COMMAND_LOCKS.get(), int(lease)))
    try:
        yield
    finally:
        copies.COMMAND_LOCKS.reset(token)


class RemotePreparationAction(Enum):
    PREPARE = "prepare"
    FINISH = "finish"
    SETTLE = "settle"


def remote_creation_command(path: Path, command: str, action: RemotePreparationAction) -> str:
    """Keep the terminal record at the same path as the creation lock."""
    program = """import fcntl, os, pathlib, sys
path = pathlib.Path.home() / sys.argv[1]
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit('Mac gate preparation or cleanup is active')
if sys.argv[2] == 'settle':
    os.pwrite(descriptor, b'closed', 0)
    os.fsync(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
elif sys.argv[2] == 'prepare' and os.fstat(descriptor).st_size:
    sys.exit('Mac gate preparation has settled')
os.environ[sys.argv[4]] = str(descriptor)
os.set_inheritable(descriptor, True)
os.execl('/bin/sh', 'sh', '-c', sys.argv[3])
"""
    return shlex.join(["python3", "-c", program, str(path), action.value, command, REMOTE_CREATION_LEASE_FD])


def remote_preparation_command(folder: Path, command: str, action: RemotePreparationAction) -> str:
    relative = Path(REMOTE_PREPARATION) / attempt_name(folder)
    guarded = remote_creation_command(relative, command, action)
    if action is not RemotePreparationAction.SETTLE:
        return guarded
    program = """import os, subprocess, sys
from scripts.agents.worker_lifecycle import operation_deadline
from scripts.mac_test.claim_state import QueueProcessUnsettled, capture_cleanup_command

budget = float(sys.argv[2])
try:
    with operation_deadline(timeout=budget) as deadline:
        os.environ[sys.argv[3]] = str(deadline)
        result = capture_cleanup_command(['/bin/sh', '-c', 'exec ' + sys.argv[1]], budget,
                                         supervise_session=True)
except (TimeoutError, subprocess.TimeoutExpired):
    sys.exit('The Mac remote checkout cleanup timed out')
except QueueProcessUnsettled as error:
    sys.exit(str(error))
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
sys.exit(result.returncode)
"""
    # Leave time for SSH to return the remote result and close.
    budget = checkout_cleanup_seconds() / 2
    return ('"$HOME/.claude/scripts/lib/py" '
            + shlex.join(['-c', program, guarded, str(budget), REMOTE_SETTLEMENT_DEADLINE]))


def upload_preparation_files(ssh: list[str], folder: Path, remote: str, files: list[tuple[Path, str]]) -> None:
    program = """import pathlib, shutil, sys
with (pathlib.Path.home() / sys.argv[1]).open("wb") as output:
    shutil.copyfileobj(sys.stdin.buffer, output)
"""
    for source, name in files:
        upload = shlex.join(['python3', '-c', program, f'{remote}/{name}'])
        guarded = remote_preparation_command(folder, upload, RemotePreparationAction.PREPARE)
        with source.open('rb') as stream:
            _ = subprocess.run([*ssh, guarded], stdin=stream, check=True, pass_fds=copies.COMMAND_LOCKS.get())


def checkout_cleanup_command(root: str, runner: str, checkout: str) -> str:
    fallback = f'printf "{CheckoutCleanup.RETAINED}\\n"'
    if Path(checkout).is_absolute():
        probe = copies.path_missing_command(checkout)
        fallback = f'if {probe}; then printf "{CheckoutCleanup.REMOVED}\\n"; else {fallback}; fi'
    return (f'if [ -f {root}/mac_gate.py ]; then {runner} remove-checkout {root}; '
            f'else rm -rf -- {root} && {fallback}; fi')


def lifecycle(phase: str, argv_file: Path, folder: Path, full: str) -> int:
    remote = f'{REMOTE_ROOT}/{attempt_name(folder)}'
    root = '"$HOME"/' + shlex.quote(remote)
    python = '/usr/bin/env PYTHONPATH="$HOME/.claude" "$HOME/.claude/scripts/lib/py"'
    runner = f'{python} {root}/mac_gate.py'
    host = os.environ['MAC_QUEUE_HOST']
    ssh = ['ssh', '-o', 'BatchMode=yes', host]
    no_input = ['ssh', '-n', *ssh[1:]]
    checkout_file = argv_file.with_suffix('.json.checkout')
    refs_journal = argv_file.with_suffix('.json.bundle-refs')
    if phase in ('cleanup', 'cleanup-ended'):
        try:
            checkout = checkout_file.read_text()
        except FileNotFoundError:
            checkout = ''
        if phase == 'cleanup-ended':
            from scripts.production.mac_gate_process import MAC_GATE_SCRIPT

            command = f'{python} "$HOME/.claude"/{MAC_GATE_SCRIPT} stop {root}'
            if Path(checkout).is_absolute():
                command += ' ' + shlex.join(['--checkout', checkout])
        else:
            command = checkout_cleanup_command(root, runner, checkout)
        cleanup = remote_preparation_command(folder, command, RemotePreparationAction.SETTLE)
        try:
            result = capture_cleanup_command([*no_input, cleanup], checkout_cleanup_seconds(),
                                             pass_fds=copies.COMMAND_LOCKS.get())
        except (subprocess.TimeoutExpired, QueueProcessUnsettled):
            print(f'{CLEANUP_ERROR_PREFIX}{folder.name}:The Mac checkout cleanup timed out', flush=True)
            return 1
        outcome = result.stdout.strip()
        if result.returncode or outcome not in (CheckoutCleanup.REMOVED, CheckoutCleanup.RETAINED):
            detail = ' '.join(result.stderr.split()) or 'The Mac did not confirm checkout cleanup'
            print(f'{CLEANUP_ERROR_PREFIX}{folder.name}:{detail}', flush=True)
            return 1
        if result.stderr:
            print(result.stderr, end='', file=sys.stderr)
        if outcome == CheckoutCleanup.REMOVED and Path(checkout).is_absolute():
            with operation_deadline(timeout=checkout_cleanup_seconds()):
                copies.unregister_remote(host, checkout)
        if refs_journal.exists():
            cloud_gate.BundleRefs.read(refs_journal).remove(bounded_bundle_git)
        return 0

    leases = copies.COMMAND_LOCKS.get()
    request = cloud_gate.read(folder / 'request.json')
    local_repo = Path((folder / 'local-repo').read_text())
    owner = copies.source_owner(local_repo)
    repository = copies.repository_name(local_repo)
    session = (os.environ.get('MAC_QUEUE_SESSION') or os.environ.get('CODEX_THREAD_ID')
               or os.environ.get('CLAUDE_CODE_SESSION_ID') or attempt_name(folder))
    files = [folder / 'request.json', folder / 'module_seconds.json']
    parent = root.rsplit('/', 1)[0]
    mkdir = f'umask 077 && mkdir -p -- {parent} && mkdir -- {root} && printf "created\n"'
    created = subprocess.check_output(
        [*no_input, remote_preparation_command(folder, mkdir, RemotePreparationAction.PREPARE)],
        text=True, pass_fds=leases)
    if created.strip() != 'created':
        raise GateRefused('Could not create the Mac gate state folder')
    upload_preparation_files(ssh, folder, remote, [(Path(__file__).resolve(), 'mac_gate.py.new')])
    clone = f'mv -- {root}/mac_gate.py.new {root}/mac_gate.py && {runner} clone {root} '
    clone += shlex.join([owner, repository, session])
    response = subprocess.check_output(
        [*no_input, remote_preparation_command(folder, clone, RemotePreparationAction.PREPARE)],
        text=True, pass_fds=leases)
    checkout = response.strip().removeprefix('mac_gate_checkout=')
    if not response.startswith('mac_gate_checkout=') or not Path(checkout).is_absolute() or '\n' in checkout:
        raise GateRefused('The Mac did not confirm the gate checkout')
    write_text_atomic(checkout_file, checkout)
    command = checkout_cleanup_command(root, runner, checkout)
    close = remote_preparation_command(folder, command, RemotePreparationAction.SETTLE)
    _ = argv_file.with_suffix('.json.close').write_text(close)
    mirror = copies.RemoteMirror(owner, copies.SelectedRepository(repository), host, checkout, session,
                                 task=checkout_task(folder))
    _ = argv_file.with_suffix('.json.lease').write_text(str(copies.remote_lease_path(mirror)))
    copies.register_remote(mirror)
    copies.reconcile_remote_mirrors(host, is_mac_gate_checkout)
    files.extend(checkout_bundle(no_input, checkout, local_repo, folder, request, journal=refs_journal))
    upload_preparation_files(ssh, folder, remote, [(path, path.name) for path in files])
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
    lock_path = folder / 'cleanup.lock'
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        opened, current = os.fstat(lock.fileno()), lock_path.stat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise GateRefused('The Mac gate checkout has already settled')
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
               cancellation: MacGateCancellation | UseCallerProcessLifetime,
               assignment: PlacementAssignment | RequestMacPlacement = REQUEST_MAC_PLACEMENT,
               ) -> MacGateResult | str:
    """Wait for a Mac placement in queue order and run the assigned modules."""
    from scripts.build_queue import admission_wire as wire, execution_identity, placement
    from scripts.production.gate_routes import MacGateCancellation

    try:
        repo = repo.resolve()
        with ExitStack() as cleanup:
            caller, lifetime = os.pipe()
            _ = cleanup.callback(os.close, caller)
            caller_lifetime = cleanup.enter_context(ExitStack())
            _ = caller_lifetime.callback(os.close, lifetime)
            caller_arguments = ['--caller-fd', str(caller)]
            caller_fds = [caller]
            if isinstance(cancellation, MacGateCancellation):
                caller_arguments.extend(['--cancel-fd', str(cancellation.caller_fd)])
                caller_fds.append(cancellation.caller_fd)
            cancelled = select.poll()
            for descriptor in caller_fds:
                cancelled.register(descriptor, select.POLLIN)
            if cancelled.poll(0):
                return 'The Mac gate was cancelled'
            paths = assigned_gate_paths(uuid.uuid4().hex)
            folder = paths.request
            placement_arguments: list[str] = []
            target = 'mac'
            encoded_assignment = ''
            if isinstance(assignment, placement.PlacementAssignment):
                if (assignment.destination.provider != 'mac' or not assignment.attempt.isascii()
                        or not assignment.attempt.isalnum()):
                    return 'The gate received an invalid Mac assignment'
                target = assignment.destination.target
                paths = assigned_gate_paths(assignment)
                folder = paths.request
                encoded_assignment = wire.encode_reply(wire.Assignment(assignment))
                placement_arguments = ['--assignment-file', str(folder / 'assignment.json'),
                                       '--gate-assignment', encoded_assignment]
            cancel_seconds = queue_cancel_seconds(target)
            command = [*QUEUE_COMMAND, *caller_arguments, '--owns-gate-request',
                       '--what', 'gate tests', '--kind', 'run', '--package', '',
                       '--worktree', str(repo), '--owned-close',
                       '--lifecycle-script', str(Path(__file__).resolve()), *placement_arguments]
            with queue_call(command, [str(folder), str(int(full))], caller_fds, caller_lifetime,
                            cancel_seconds) as (process, preparation):
                preparation.settimeout(cancel_seconds)
                if preparation.recv(1) != b'1':
                    output, _ = process.communicate(timeout=cancel_seconds)
                    return ('The Mac queue did not take ownership of the gate request'
                            + (f'. {output.strip()}' if output.strip() else ''))
                if cancelled.poll(0):
                    return 'The Mac gate was cancelled'
                with gate_preparation_lock(paths.preparation_lock):
                    if not paths.journal.exists():
                        return 'The Mac gate journal has ended'
                    folder.mkdir()
                    if isinstance(assignment, placement.PlacementAssignment):
                        write_text_atomic(folder / 'assignment.json', encoded_assignment)
                    request = cloud_gate.prepare_request(repo, folder, mode, module_time_limit_s, selected=selected)
                    if (isinstance(assignment, placement.PlacementAssignment)
                            and assignment.source_key != execution_identity.SourceKeyKnown(ledger.text(request, 'tree'))):
                        return 'The checkout tree differs from the Mac assignment'
                    cloud_gate.save(folder / 'request.json', request)
                    _ = (folder / 'local-repo').write_text(str(repo))
                    if not request['modules']:
                        return 'No modules selected for the Mac'
                    timings = gate.read_measures(gate.gate_state_dir() / 'module_seconds.json')
                    _ = (folder / 'module_seconds.json').write_text(json.dumps(timings))
                    # Keep the receipts open after the queue removes the request.
                    refusal_receipt = cleanup.enter_context((folder / ASSIGNED_REFUSAL).open('w+'))
                    completion_receipt = cleanup.enter_context((folder / ASSIGNED_COMPLETION).open('w+'))
                    if cancelled.poll(0):
                        return 'The Mac gate was cancelled'
                preparation.sendall(b'1')
                preparation.close()
                output, _ = process.communicate(timeout=MAC_TIME_LIMIT_S)
            lines = output.splitlines()
            was_cancelled = bool(cancelled.poll(0))
            status = process.returncode
            assert status is not None
            refusal_text = refusal_receipt.read()
            if isinstance(assignment, placement.PlacementAssignment) and status and not was_cancelled and refusal_text:
                refusal = wire.parse_reply(refusal_text)
                if (isinstance(refusal, wire.DispatchStatus) and refusal.assignment == assignment
                        and isinstance(refusal.state, placement.ClosedAssignment)
                        and refusal.retry == placement.DoNotRetry(refusal.state.reason)):
                    raise SettledMacRefusal(refusal.state.reason)
            if status or was_cancelled:
                return queue_failure(lines, folder, status, cancelled=was_cancelled)
            result_lines = MacGateResultLines()
            for line in lines:
                result_lines.record(line)
            result = result_lines.decode(request)
            if isinstance(assignment, placement.PlacementAssignment):
                if completion_receipt.read() != assignment.attempt:
                    return 'The service did not confirm Mac gate settlement'
            return result
    except subprocess.TimeoutExpired:
        return f'The Mac queue call exceeded {MAC_TIME_LIMIT_S:g} seconds'
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, GateRefused, GraphError,
            copies.CopyError, scratch.ScratchError, wire.AdmissionUnavailable) as error:
        return f'The Mac gate could not finish: {error}'


def run_assigned(repo: Path, *, assignment: PlacementAssignment, mode: gate.SuiteMode, full: bool,
                 module_time_limit_s: float, selected: gate.TreeSelection,
                 ) -> MacGateCompleted | MacGatePrelaunchRefused | MacGateUnresolved:
    """Return the result or the settlement state of the exact Mac assignment."""
    from scripts.production.gate_routes import MacGateCompleted, MacGatePrelaunchRefused, MacGateUnresolved

    try:
        outcome = run_remote(repo, mode=mode, full=full, module_time_limit_s=module_time_limit_s, selected=selected,
                             cancellation=USE_CALLER_PROCESS_LIFETIME, assignment=assignment)
    except SettledMacRefusal as refusal:
        return MacGatePrelaunchRefused(str(refusal))
    if isinstance(outcome, MacGateResult):
        return MacGateCompleted(outcome.run)
    return MacGateUnresolved(outcome)


def queue_failure(lines: list[str], folder: Path, status: int, *, cancelled: bool) -> str:
    prefix = f'{CLEANUP_ERROR_PREFIX}{folder.name}:'
    cleanup = next((line.removeprefix(prefix).strip() for line in reversed(lines) if line.startswith(prefix)), '')
    reason = cleanup or ('The Mac gate was cancelled' if cancelled else f'The Mac queue call ended with status {status}')
    detail = next((line.strip().removeprefix(prefix).strip() for line in reversed(lines) if line.strip()), '')
    return f'{reason}: {detail}' if detail and detail != reason and not detail.startswith(RESULT_PREFIX) else reason


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
    for phase in ('stop', 'remove-checkout'):
        stop = commands.add_parser(phase)
        _ = stop.add_argument('folder', type=Path)
        _ = stop.add_argument('--checkout', type=Path)
    args = parser.parse_args(namespace=Arguments())
    try:
        if args.action in ('prepare', 'cleanup', 'cleanup-ended'):
            with hold_gate_creation_commands():
                return lifecycle(args.action, args.argv_file, args.folder, args.full)
        if args.action == 'clone':
            path = clone_checkout(args.folder, args.owner, args.repository, args.session)
            print(f'mac_gate_checkout={path}', flush=True)
        elif args.action == 'stage':
            stage_checkout(args.folder, args.full)
        elif args.action == 'guest':
            print(RESULT_PREFIX + json.dumps(run_guest(args.folder, full=args.full == '1')), flush=True)
        elif args.action == 'stop':
            print(supervise_checkout_cleanup(args.folder, args.checkout))
        elif args.action == 'remove-checkout':
            print(stop_checkout(args.folder, args.checkout))
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

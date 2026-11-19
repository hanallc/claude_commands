#!/usr/bin/env python3
"""Run gate modules through the Mac queue and compare their counts with natedev."""
from __future__ import annotations

import argparse
from collections.abc import Generator
from contextlib import contextmanager, redirect_stdout, suppress
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import cast

if __package__ in ('', None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.aws_target import cloud_gate, gate_box, ledger
from scripts.buildlog import store
from scripts.copies import copies
from scripts.lib import conf, copy_record, process_group, scratch
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_start import Gone, Started, Unreadable, process_start
from scripts.mac_test.mac_queue_run import MAC_BUSY_STATUS
from scripts.production import merge_gate as gate
from scripts.production.affected_modules import GraphError
from scripts.production.gate_modules import GateRefused, discover_test_modules
from scripts.test_times import gate_record

MODULE_LIST = Path('config/mac_gate_modules.txt')
REMOTE_ROOT = '.local/state/mac-gate'
RESULT_PREFIX = 'mac_gate_result='
QUEUE_COMMAND = (sys.executable, str(Path(__file__).with_name('mac_queue_run.py')))
MAC_TIME_LIMIT_S = 3600


@dataclass(frozen=True)
class MacGateResult:
    row: dict[str, object]
    run: gate.GateRun


def read_modules(repo: Path) -> tuple[str, ...]:
    """Return listed test paths in the tested checkout."""
    try:
        entries = set((repo / MODULE_LIST).read_text().split())
    except FileNotFoundError:
        return ()
    if not entries:
        return ()
    included = set(gate.checked_git(repo, 'ls-files', '-z', '--cached', '--others',
                                    '--exclude-standard', '--', 'scripts').split('\0'))
    allowed = entries & included
    return tuple(name for _, path in discover_test_modules(repo)
                 if (name := path.relative_to(repo).as_posix()) in allowed)


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


def stop_guest(folder: Path) -> None:
    """Stop the guest and the process groups it recorded."""
    journal = folder / 'run.json'
    if not journal.exists():
        return
    grace = conf.read('mac_gate').number('terminate_grace_seconds')
    record = cloud_gate.read(journal)
    pid, start = ledger.instant(record, 'pid'), ledger.text(record, 'start')
    if pid <= 1 or pid == os.getpid():
        raise GateRefused('Invalid Mac gate owner')
    if running_owner(pid, start):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        until = time.monotonic() + grace
        while running_owner(pid, start):
            if time.monotonic() >= until:
                raise GateRefused('The Mac gate guest did not exit')
            time.sleep(0.05)

    groups: set[int] = set()
    for group, stamp in ledger.mapping(cloud_gate.read(journal)['groups']).items():
        pgid = int(group)
        identity = process_start(pgid)
        if isinstance(identity, Unreadable):
            raise GateRefused(f'Cannot read the Mac gate group {pgid}: {identity.reason}')
        if isinstance(identity, Gone) or identity.start == stamp:
            groups.add(process_group.check_process_group(pgid))
    for signum in (signal.SIGTERM, signal.SIGKILL):
        for pgid in live_groups(groups):
            with suppress(ProcessLookupError):
                process_group.send_process_group(pgid, signum)
        until = time.monotonic() + grace
        while live_groups(groups):
            if time.monotonic() >= until:
                break
            time.sleep(0.05)
        else:
            return
    raise GateRefused('The Mac gate job groups did not exit')


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
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not folder.exists():
            return
        stop_guest(folder)
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


def lifecycle(phase: str, argv_file: Path, folder: Path, full: str) -> int:
    remote = f'{REMOTE_ROOT}/{attempt_name(folder)}'
    root = '"$HOME"/' + shlex.quote(remote)
    runner = f'PYTHONPATH="$HOME/.claude" "$HOME/.claude/scripts/lib/py" {root}/mac_gate.py'
    host = os.environ['MAC_QUEUE_HOST']
    ssh = ['ssh', '-n', '-o', 'BatchMode=yes', host]
    checkout_file = argv_file.with_suffix('.json.checkout')
    cleanup = (f'if [ -f {root}/mac_gate.py ]; then {runner} stop {root}; '
               f'else rm -rf -- {root}; fi')
    if phase == 'cleanup':
        result = subprocess.run([*ssh, cleanup + ' && printf "mac_gate_cleaned\n"'],
                                capture_output=True, text=True, check=False)
        if result.returncode or result.stdout.strip() != 'mac_gate_cleaned':
            if result.stderr:
                print(result.stderr, file=sys.stderr, end='')
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
    commit = ledger.text(request, 'commit')
    probe = shlex.join(['git', '-C', checkout, 'cat-file', '-e', commit + '^{commit}'])
    if subprocess.check_output([*ssh, probe + ' 2>/dev/null && printf "present\n"'], text=True).strip() != 'present':
        if not request['source_key']:
            raise GateRefused('The Mac checkout does not have the requested commit')
        files.append(folder / 'source.bundle')
    _ = subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', *map(str, files), f'{host}:{remote}/'], check=True)
    _ = argv_file.write_text(json.dumps(['/bin/zsh', '-c', f'exec {runner} stage {root} {full}']))
    return 0


def stage_checkout(folder: Path, full: str) -> None:
    """Set the tested commit and base in the task-owned checkout."""
    request = cloud_gate.read(folder / 'request.json')
    commit, base = (ledger.text(request, key) for key in ('commit', 'origin_main'))
    repo = read_checkout(folder)
    with record_guest(folder, repo):
        _ = gate.checked_git(repo, 'cat-file', '-e', base + '^{commit}')
        if gate.git(repo, 'cat-file', '-e', commit + '^{commit}').returncode:
            _ = gate.checked_git(repo, 'fetch', '-q', str(folder / 'source.bundle'), commit)
        _ = gate.checked_git(repo, 'remote', 'set-url', 'origin', ledger.text(request, 'origin'))
        _ = gate.checked_git(repo, 'update-ref', 'refs/remotes/origin/main', base)
        _ = gate.checked_git(repo, 'checkout', '-q', '--detach', commit)
        shim = str(repo / 'scripts/lib/py')
        os.execv(shim, [shim, str(repo / 'scripts/mac_test/mac_gate.py'), 'guest', str(folder), full])


@contextmanager
def record_guest(folder: Path, repo: Path) -> Generator[None]:
    pid = os.getpid()
    owner = process_start(pid)
    if not isinstance(owner, Started):
        raise GateRefused('The Mac gate guest has no readable process start')
    recovery = gate.GateRecovery(folder / 'run.json', str(repo.resolve()), pid, owner.start)
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
               selected: gate.AllModules | gate.TreeSelection) -> MacGateResult | str:
    try:
        repo = repo.resolve()
        with tempfile.TemporaryDirectory(prefix='mac-gate-') as temporary:
            folder = Path(temporary)
            request = cloud_gate.prepare(repo, folder, mode, module_time_limit_s, selected=selected)
            _ = (folder / 'local-repo').write_text(str(repo))
            if not request['modules']:
                return 'No modules selected for the Mac'
            timings = gate.read_measures(gate.gate_state_dir() / 'module_seconds.json')
            _ = (folder / 'module_seconds.json').write_text(json.dumps(timings))
            command = [*QUEUE_COMMAND, '--what', 'gate tests', '--kind', 'run', '--package', '',
                       '--worktree', str(repo), '--no-wait', '--lifecycle-script', str(Path(__file__).resolve()),
                       '--', str(folder), str(int(full))]
            caller = os.pidfd_open(os.getpid())
            try:
                command[2:2] = ['--caller-fd', str(caller)]
                process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, encoding='utf-8',
                                           errors='replace', start_new_session=True, pass_fds=(caller,))
            finally:
                os.close(caller)
            with process:
                try:
                    output, _ = process.communicate(timeout=MAC_TIME_LIMIT_S)
                except BaseException:
                    with suppress(ProcessLookupError):
                        process_group.send_process_group(process.pid, signal.SIGKILL)
                    raise
            if process.returncode == MAC_BUSY_STATUS:
                return 'The Mac is busy'
            if process.returncode:
                return f'The Mac queue call ended with status {process.returncode}'
            results = [line.removeprefix(RESULT_PREFIX) for line in output.splitlines() if line.startswith(RESULT_PREFIX)]
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


def route(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
          selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES) -> gate.GateRun | str:
    """Return the observed module results or a reason to run them on natedev."""
    outcome = run_remote(repo, mode=mode, full=full, module_time_limit_s=module_time_limit_s, selected=selected)
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
    passed: list[str] = []
    for job in gate_box.decode_run(row['gate']).jobs:
        name = f'scripts/{job.suite}/{job.module}'
        if (job.kind is gate.GateJobKind.TEST_MODULE and job.returncode == 0 and not job.killed_for
                and isinstance(job.times, dict)
                and baseline.get(name) == ModuleCounts(ledger.instant(ledger.mapping(job.times), 'tests_run'),
                                                       ledger.instant(ledger.mapping(job.times), 'skipped'))):
            passed.append(name)
    write_text_atomic(repo / MODULE_LIST, ''.join(name + '\n' for name in sorted(set(passed))))


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
    writer = commands.add_parser('write-list', help='compare a Mac result with a natedev gate log')
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
                                module_time_limit_s=gate.DEFAULT_MODULE_TIME_LIMIT_S, selected=gate.ALL_MODULES)
            if isinstance(result, str):
                raise GateRefused(result)
            cloud_gate.save(args.output, result.row)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, GateRefused, GraphError, copies.CopyError, scratch.ScratchError) as error:
        print(f'Mac gate: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

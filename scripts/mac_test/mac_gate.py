#!/usr/bin/env python3
"""Run gate modules through the Mac queue and compare their counts with natedev."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout, suppress
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import cast

if __package__ in ('', None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.aws_target import cloud_gate, gate_box, ledger
from scripts.buildlog import store
from scripts.lib import process_group
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_start import Started, process_start
from scripts.lint import folder_audit
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


def stop_checkout(folder: Path) -> None:
    """Stop processes that use this attempt's checkout before removing it."""
    root = (Path.home() / REMOTE_ROOT).resolve()
    if folder.resolve() != root / attempt_name(folder):
        raise GateRefused('The folder is outside the Mac gate state directory')
    repo = folder.resolve() / 'repo'
    if repo.is_symlink():
        raise GateRefused('The Mac gate checkout is a symbolic link')
    if repo.is_dir():
        def users() -> set[int]:
            uses = folder_audit.process_uses([repo])
            if isinstance(uses, folder_audit.ProcessUsesUnavailable):
                raise GateRefused('Cannot check for test processes on the Mac')
            return {pid for path, pid, _ in uses.paths if pid > 1 and pid != os.getpid()
                    and Path(path).is_relative_to(repo)}

        processes = {pid: process_start(pid) for pid in users()}
        if processes:
            for pid in users() & processes.keys():
                identity = processes[pid]
                if isinstance(identity, Started) and process_start(pid) == identity:
                    with suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
            if users():
                raise GateRefused('Test processes still use the Mac checkout')
    shutil.rmtree(folder, ignore_errors=False)


def lifecycle(phase: str, argv_file: Path, folder: Path, full: str) -> int:
    # The saved queue arguments supply the same remote path after a local timeout.
    remote = f'{REMOTE_ROOT}/{attempt_name(folder)}'
    root = '"$HOME"/' + shlex.quote(remote)
    repo = root + '/repo'
    runner = repo + '/scripts/lib/py ' + repo + '/scripts/mac_test/mac_gate.py'
    host = os.environ['MAC_QUEUE_HOST']
    ssh = ['ssh', '-n', '-o', 'BatchMode=yes', host]
    cleanup = (f'if [ -f {repo}/scripts/mac_test/mac_gate.py ]; then {runner} stop {root}; '
               f'else rm -rf -- {root}; fi')
    if phase == 'cleanup':
        result = subprocess.run([*ssh, cleanup + ' && printf "mac_gate_cleaned\\n"'],
                                capture_output=True, text=True, check=False)
        return int(result.returncode != 0 or result.stdout.strip() != 'mac_gate_cleaned')

    request = cloud_gate.read(folder / 'request.json')
    files = [folder / 'request.json', folder / 'module_seconds.json']
    if request['source_key']:
        files.append(folder / 'source.bundle')
    _ = argv_file.with_suffix('.json.close').write_text(cleanup)
    parent = root.rsplit('/', 1)[0]
    _ = subprocess.run([*ssh, f'umask 077 && mkdir -p -- {parent} && mkdir -- {root}'], check=True)
    _ = subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', *map(str, files), f'{host}:{remote}/'], check=True)

    commit, origin, base = (shlex.quote(ledger.text(request, key)) for key in ('commit', 'origin', 'origin_main'))
    git = f'git -C {repo}'
    commands = [
        f'git clone -q --no-checkout "$HOME/.claude" {repo}',
        f'{git} remote set-url origin {origin}',
        f'GIT_TERMINAL_PROMPT=0 {git} fetch -q origin +refs/heads/main:refs/remotes/origin/main',
        f'{git} update-ref refs/remotes/origin/main {base}',
    ]
    if request['source_key']:
        commands.append(f'{git} fetch -q {root}/source.bundle {commit}')
    commands.extend((f'{git} checkout -q --detach {commit}', f'{runner} guest {root} {full}'))
    _ = argv_file.write_text(json.dumps(['/bin/zsh', '-c', ' && '.join(commands)]))
    return 0


def run_guest(folder: Path, *, full: bool) -> dict[str, object]:
    repo = folder / 'repo'
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
            if not request['modules']:
                return 'No modules selected for the Mac'
            timings = gate.read_measures(gate.gate_state_dir() / 'module_seconds.json')
            _ = (folder / 'module_seconds.json').write_text(json.dumps(timings))
            command = [*QUEUE_COMMAND, '--what', 'gate tests', '--kind', 'run', '--package', '',
                       '--worktree', str(repo), '--no-wait', '--lifecycle-script', str(Path(__file__).resolve()),
                       '--', str(folder), str(int(full))]
            with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, encoding='utf-8',
                                  errors='replace', start_new_session=True) as process:
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
            return MacGateResult(row, cloud_gate.validate_run_identity(row, request))
    except subprocess.TimeoutExpired:
        return f'The Mac queue call exceeded {MAC_TIME_LIMIT_S:g} seconds'
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, GateRefused, GraphError) as error:
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
    guest = commands.add_parser('guest')
    _ = guest.add_argument('folder', type=Path)
    _ = guest.add_argument('full', choices=('0', '1'))
    _ = commands.add_parser('stop').add_argument('folder', type=Path)
    args = parser.parse_args(namespace=Arguments())
    try:
        if args.action in ('prepare', 'cleanup'):
            return lifecycle(args.action, args.argv_file, args.folder, args.full)
        if args.action == 'guest':
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
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, GateRefused, GraphError) as error:
        print(f'Mac gate: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

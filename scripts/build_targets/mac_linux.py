"""Run the gate in the Mac's Linux guest."""
from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext, redirect_stdout
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
import json
import math
from pathlib import Path
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
from typing import Literal, TypedDict, cast
from urllib.parse import urlsplit
from uuid import uuid4

from scripts.aws_target import gate_box, ledger
from scripts.build_queue.admission_jobs import GateJobNotRecorded, StepJob
from scripts.lib import conf
from scripts.production.gate_modules import GateRefused, discover_test_modules
from scripts.production.gate_routes import AllModules, GateJobKind, GateRun, ModuleCoverage, SuiteMode, TreeSelection


TARGET = 'mac-linux'
ORIGIN_MAIN = 'refs/remotes/origin/main'
SSH = ('ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ForwardAgent=no',
       '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2')
# Give the transfer, checkout, and result time outside the guest runner deadline.
TRANSFER_ALLOWANCE_SECONDS = 120


class GuestUnavailable(RuntimeError):
    """The guest cannot return a completed gate."""


class GuestSwitch(StrEnum):
    ON = 'on'
    OFF = 'off'


@dataclass(frozen=True)
class GuestSettings:
    ssh_hops: tuple[str, str]
    guest_user: str
    jobs: int
    run_time_limit_seconds: int
    zstd_level: int

    def command(self, shell: str) -> tuple[str, ...]:
        mac, guest = self.ssh_hops
        return (*SSH, mac, shlex.join((*SSH, '-l', self.guest_user, guest, shlex.join(('bash', '-c', shell)))))


def read_settings(registry: conf.ConfigRead) -> GuestSettings:
    if (registry.text(f'kind.{TARGET}', recover_invalid=False) != 'ssh'
            or registry.text(f'platform.{TARGET}', recover_invalid=False) != 'linux'):
        raise GuestUnavailable(f'{TARGET} must use SSH and the Linux platform')
    settings_file = Path(registry.text(f'settings.{TARGET}', recover_invalid=False)).expanduser()
    settings = conf.read(registry.source.parent / settings_file)
    hops = settings.value('ssh.hops', str.split, recover_invalid=False)
    user = settings.text('guest.user', recover_invalid=False)
    jobs = settings.value('test.jobs', int, recover_invalid=False)
    limit = settings.value('run.time_limit_seconds', int, recover_invalid=False)
    zstd_level = settings.value('copy.zstd_level', int, recover_invalid=False)
    if len(hops) != 2 or not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', host) for host in hops):
        raise GuestUnavailable('ssh.hops must name two SSH aliases')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', user):
        raise GuestUnavailable('guest.user must name a Linux user')
    if jobs < 1 or limit < 1:
        raise GuestUnavailable('test.jobs and run.time_limit_seconds must be positive')
    if not 1 <= zstd_level <= 19:
        raise GuestUnavailable('copy.zstd_level must be between 1 and 19')
    return GuestSettings((hops[0], hops[1]), user, jobs, limit, zstd_level)


def mac_linux_reservation(_repo: Path, _work: SuiteMode | StepJob,
                          _gate_job: StepJob | GateJobNotRecorded) -> AbstractContextManager[None]:
    """Keep the reservation through the guest attempt.

    B3 adds scripts.build_queue.whole_run.held(repo: Path, target: str, work: SuiteMode | StepJob, gate_job).
    The context manager yields WholeRun. Set its integer status as for CloudReservation.status.
    On whole_run.WholeRunRefused, return str(error) as the route fallback.
    The whole-Mac claim awaits Nate's approval.
    """
    # TODO(item 191 B3): Reserve mac-linux through the queue before the guest attempt.
    return nullcontext()


class GuestRequest(TypedDict):
    attempt: str
    commit: str
    tree: str
    mode: str
    full: bool
    selection: list[str] | Literal['all']
    module_time_limit_s: float
    run_time_limit_seconds: int
    jobs: int
    unit: str


@dataclass(frozen=True)
class CandidateSource:
    head: str
    commit: str
    tree: str
    origin_url: str
    modules: frozenset[str]
    coverage: ModuleCoverage


def candidate_source(repo: Path, selected: AllModules | TreeSelection) -> CandidateSource:
    from scripts.aws_target import cloud_gate
    from scripts.production.gate import checked_git

    head = checked_git(repo, 'rev-parse', 'HEAD')
    tree, dirty = cloud_gate.working_tree(repo)
    paths = frozenset(str(path.relative_to(repo)) for _, path in discover_test_modules(repo))
    modules = paths
    if isinstance(selected, TreeSelection):
        if selected.tree != tree:
            raise GuestUnavailable('the checkout changed after module selection')
        modules = frozenset(selected.paths)
        if not modules <= paths:
            raise GuestUnavailable('selected modules are absent from the checkout')
    origin_url = checked_git(repo, 'remote', 'get-url', 'origin')
    origin = urlsplit(origin_url)
    if origin.password or origin.query or ('://' in origin_url and origin.username):
        raise GuestUnavailable('the origin URL must contain no credential')
    commit = checked_git(repo, 'commit-tree', tree, '-p', head, '-m', 'Mac Linux gate snapshot') if dirty else head
    coverage = ModuleCoverage.COMPLETE if modules == paths else ModuleCoverage.SUBSET
    return CandidateSource(head, commit, tree, origin_url, modules, coverage)


def compressed_bootstrap(request: GuestRequest, level: int) -> bytes:
    manifest = json.dumps(request).encode('utf-8')
    record = tarfile.TarInfo('request.json')
    record.size = len(manifest)
    with BytesIO() as bootstrap:
        with tarfile.open(fileobj=bootstrap, mode='w') as archive:
            archive.addfile(record, BytesIO(manifest))
            archive.add(Path(__file__).resolve(), arcname='runner.py')
        return subprocess.run(['zstd', f'-{level}', '-q', '-c'], input=bootstrap.getvalue(),
                              capture_output=True, check=True, timeout=TRANSFER_ALLOWANCE_SECONDS).stdout


def run_shell(settings: GuestSettings, source: CandidateSource, bootstrap_bytes: int) -> str:
    return f'''set -euo pipefail
if [ "$(id -un)" != {shlex.quote(settings.guest_user)} ]; then
    printf 'Expected SSH account %s.\\n' {shlex.quote(settings.guest_user)} >&2
    exit 1
fi
umask 077
checkout="$HOME/.claude"
if [ -L "$checkout" ] || [ -L "$checkout/.git" ]; then
    printf 'The guest main checkout must be a real directory.\\n' >&2
    exit 1
fi
mkdir -p -- "$checkout"
cd "$checkout"
exec 9< .
flock -x 9
if [ ! -f .git/mac-linux-owned ]; then
    if [ -n "$(find . -mindepth 1 ! -path './.git' -print -quit)" ]; then
        printf 'The guest main checkout belongs to another tool.\\n' >&2
        exit 1
    fi
    mkdir -p -- .git
    touch .git/mac-linux-owned
fi
export PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
export CLAUDE_DISABLE_DATACLASS_CODE_CACHE=1
export USER={shlex.quote(settings.guest_user)} LOGNAME={shlex.quote(settings.guest_user)}
export USERNAME="$USER" LNAME="$USER"
unset DBUS_SESSION_BUS_ADDRESS SSH_AUTH_SOCK SSH_AGENT_PID SUDO_USER SUDO_UID SUDO_GID
unset XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME XDG_STATE_HOME
unset GH_CONFIG_DIR GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN
unset CLAUDE_CONFIG_DIR CODEX_HOME CONF_DIR PYTHONPATH PYTHONHOME
git init -q
git config core.logAllRefUpdates false
git config remote.origin.url {shlex.quote(source.origin_url)}
git symbolic-ref HEAD refs/heads/main
git symbolic-ref refs/remotes/origin/HEAD {ORIGIN_MAIN}
bootstrap="$checkout/.git/mac-linux"
rm -rf -- "$bootstrap"
mkdir -p -- "$bootstrap/tmp"
installing=no
cleanup() {{
    if [ "$installing" = yes ]; then
        if git rev-parse --verify -q HEAD >/dev/null; then
            git reset --hard -q HEAD
        else
            git read-tree --empty
        fi
        git clean -ffdx -q
    fi
    rm -rf -- "$bootstrap"
}}
trap cleanup EXIT
export TMPDIR="$bootstrap/tmp" XDG_CACHE_HOME="$bootstrap/cache"
head -c {bootstrap_bytes} | zstd -d -q | tar -xf - -C "$bootstrap"
installing=yes
git read-tree --empty
git clean -ffdx -q
zstd -d -q | tar -xf -
git -c core.autocrlf=false -c core.filemode=true add --force --all
tree=$(git write-tree)
if [ "$tree" != {shlex.quote(source.tree)} ]; then
    printf 'The archive tree %s differs from the requested tree.\\n' "$tree" >&2
    exit 1
fi
commit=$(git -c user.name='Mac Linux gate' -c user.email='mac-linux@localhost' commit-tree "$tree" -m 'Mac Linux gate source')
printf 'update refs/heads/main %s\\nupdate {ORIGIN_MAIN} %s\\n' "$commit" "$commit" | git update-ref --stdin
installing=no
umask 022
./scripts/lib/py "$bootstrap/runner.py" "$bootstrap/request.json"'''


@dataclass(frozen=True)
class GuestReply:
    result: str
    compressed_source_bytes: int


# Send the SSH result to descriptor 3. Use descriptor 4 to collect the byte count.
STREAM_SOURCE = '''
exec 3>&1
size=$(
    exec 4>&1
    { cat && git archive --format=tar "$1" | zstd "$2" -T0 -q -c; } |
        tee >(wc -c >&4) | "${@:3}" >&3 3>&- 4>&-
) || exit "$?"
printf '\\n%s\\n' "$size"
'''


def run_remote(repo: Path, settings: GuestSettings, source: CandidateSource, request: GuestRequest) -> GuestReply:
    bootstrap = compressed_bootstrap(request, settings.zstd_level)
    timeout = settings.run_time_limit_seconds + TRANSFER_ALLOWANCE_SECONDS
    command = settings.command(run_shell(settings, source, len(bootstrap)))
    with subprocess.Popen(['bash', '-o', 'pipefail', '-c', STREAM_SOURCE, TARGET, source.tree,
                           f'-{settings.zstd_level}', *command], cwd=repo, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as stream:
        try:
            output, errors = stream.communicate(bootstrap, timeout=timeout)
        except BaseException as error:
            try:
                os.killpg(stream.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if isinstance(error, subprocess.TimeoutExpired):
                raise GuestUnavailable(f'SSH or guest run exceeded {timeout} seconds') from error
            raise
        if stream.returncode:
            detail = ' '.join(errors.decode(errors='replace').split())[-1200:]
            raise GuestUnavailable(f'SSH or source stream exited {stream.returncode}: {detail or "no diagnostic"}')
    result, _, count = output.rstrip().rpartition(b'\n')
    return GuestReply(result.decode('utf-8'), int(count) - len(bootstrap))


@dataclass(frozen=True, kw_only=True)
class MacLinuxGateRun(GateRun):
    """Keep the source size and the skip counts that the guest measured."""

    compressed_bytes: int
    module_skips: dict[str, int]


def completed_run(text: str, request: GuestRequest, source: CandidateSource, compressed_bytes: int) -> MacLinuxGateRun:
    row = ledger.mapping(cast(object, json.loads(text)))
    for key in ('attempt', 'commit', 'tree'):
        if row.get(key) != request[key]:
            raise GuestUnavailable(f'the guest result has a different {key}')
    run = gate_box.decode_run(row['gate'])
    modules = tuple(f'scripts/{job.suite}/{job.module}' for job in run.jobs)
    if (len(modules) != len(source.modules) or frozenset(modules) != source.modules
            or any(job.kind is not GateJobKind.TEST_MODULE for job in run.jobs)):
        raise GuestUnavailable('the guest result has a different module list')
    if row.get('verdict') != ('pass' if run.passed else 'fail'):
        raise GuestUnavailable('the guest result has an invalid verdict')
    skips = {path: job.times['skipped'] for path, job in zip(modules, run.jobs, strict=True)
             if isinstance(job.times, dict)}
    return MacLinuxGateRun(run.jobs, run.seconds, run.peak_memory_mib, run.tests_reused, run.selection_certified,
                           run.executions, run.time_limit_report, coverage=source.coverage,
                           compressed_bytes=compressed_bytes, module_skips=skips)


def route(repo: Path, *, mode: SuiteMode, full: bool, module_time_limit_s: float,
          selected: AllModules | TreeSelection, gate_job: StepJob | GateJobNotRecorded) -> GateRun | str:
    """Return the guest gate result or a reason for the natedev fallback."""
    from scripts.aws_target import cloud_gate
    from scripts.build_queue.admission_client import step_identity
    from scripts.production import affected_modules, gate

    try:
        registry = conf.read('build_targets')
        if registry.value(f'enabled.{TARGET}', GuestSwitch, recover_invalid=False) is GuestSwitch.OFF:
            return f'{TARGET} is off (build_targets.conf enabled.{TARGET}=off)'
        settings = read_settings(registry)
        if not math.isfinite(module_time_limit_s) or module_time_limit_s <= 0:
            raise GuestUnavailable('the module time limit must be positive and finite')
        repo = repo.resolve()
        unit = gate_job.unit if isinstance(gate_job, StepJob) else ''
        if not unit:
            unit = step_identity(mode.value, str(repo)).unit
        if not unit:
            raise GuestUnavailable('the gate has no owner unit for a numbered copy')
        source = candidate_source(repo, selected)
        request = GuestRequest(attempt=uuid4().hex, commit=source.commit, tree=source.tree, mode=mode.wire, full=full,
                               selection=list(selected.paths) if isinstance(selected, TreeSelection) else 'all',
                               module_time_limit_s=module_time_limit_s, run_time_limit_seconds=settings.run_time_limit_seconds,
                               jobs=settings.jobs, unit=unit)
        with mac_linux_reservation(repo, mode, gate_job):
            reply = run_remote(repo, settings, source, request)
            run = completed_run(reply.result, request, source, reply.compressed_source_bytes)
        if gate.checked_git(repo, 'rev-parse', 'HEAD') != source.head or cloud_gate.working_tree(repo)[0] != source.tree:
            raise GuestUnavailable('the checkout changed during the guest run')
        return run
    except (GuestUnavailable, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError,
            GateRefused, affected_modules.GraphError) as error:
        return f'{TARGET}: {error}'


def run_guest(request_path: Path) -> int:
    """Take a numbered copy before starting the guest gate."""
    from scripts.lib import build_folders, build_slots

    request = guest_request(request_path)
    main = Path.cwd()
    with build_slots.take_copy(main, 'HEAD', showrunner='', unit=request['unit'],
                               work=request['mode'], purpose=TARGET,
                               base_policy=build_slots.CopyBasePolicy.REPLACE) as copy:
        with build_folders.tool_folder(TARGET, copy.cargo_dir) as runtime:
            home = runtime / 'home'
            (home / '.claude').mkdir(parents=True)
            (home / '.claude/config').symlink_to(copy.path / 'config', target_is_directory=True)
            browser = shutil.which('chromium')
            if browser is None:
                raise GuestUnavailable('Chromium is absent from the guest PATH.')
            binaries = runtime / 'bin'
            binaries.mkdir()
            (binaries / 'google-chrome').symlink_to(browser)
            runtime_dir = runtime / 'xdg'
            runtime_dir.mkdir(mode=0o700)
            tmp = runtime / 'tmp'
            tmp.mkdir()
            environment = {**os.environ, **copy.env, 'HOME': str(home), 'XDG_RUNTIME_DIR': str(runtime_dir),
                           'XDG_CACHE_HOME': str(home / '.cache'), 'TMPDIR': str(tmp),
                           'CARGO_HOME': str(home / '.cargo'), 'RUSTUP_HOME': str(home / '.rustup'),
                           'PATH': str(binaries) + os.pathsep + os.environ['PATH']}
            # The -c entry selects the numbered copy as the Python checkout.
            command = ['./scripts/lib/py', '-X', f'cpu_count={request["jobs"]}',
                       '-c', 'import runpy, sys; runpy.run_path(sys.argv[1], run_name="__main__")',
                       str(Path(__file__).resolve()), str(request_path), '--run-copy']
            if shutil.which('dbus-run-session'):
                command[:0] = ['dbus-run-session', '--']
            return subprocess.run(command, cwd=copy.path, env=environment,
                                  pass_fds=build_folders.held_build_folder_descriptors(),
                                  stdin=subprocess.DEVNULL, check=False).returncode


def guest_request(path: Path) -> GuestRequest:
    return cast(GuestRequest, json.loads(path.read_text(encoding='utf-8')))


def run_copy(request_path: Path) -> None:
    """Run the candidate's gate and write one JSON result to standard output."""
    from scripts.copies import copies
    from scripts.lib import build_folders
    from scripts.production import gate
    from scripts.production.gate_routes import ALL_MODULES, SelectedModules
    from scripts.test_times.gate_record import GateTimeLimit

    repo = Path.cwd()
    request = guest_request(request_path)
    if os.cpu_count() != request['jobs']:
        raise GuestUnavailable(f'The guest Python did not set the CPU count to {request["jobs"]}.')
    tree = gate.checked_git(repo, 'rev-parse', 'HEAD^{tree}')
    if tree != request['tree']:
        raise GuestUnavailable('The guest checkout differs from the request.')
    selection = request['selection']
    machine = gate.WholeMachine(gate_parent=build_folders.ExplicitBuildParent(Path(os.environ['TMPDIR'])))
    token = copies.COMMAND_LOCKS.set((int(os.environ[build_folders.NUMBERED_COPY_LOCK_ENV]),))
    try:
        with redirect_stdout(sys.stderr):
            run = gate.run_suites(repo, full=request['full'], mode=SuiteMode(request['mode']), machine=machine,
                                  selected=ALL_MODULES if selection == 'all' else SelectedModules(tuple(selection)),
                                  module_time_limit_s=request['module_time_limit_s'],
                                  time_limit=GateTimeLimit(request['run_time_limit_seconds']))
    finally:
        copies.COMMAND_LOCKS.reset(token)
    print(json.dumps({'attempt': request['attempt'], 'commit': request['commit'], 'tree': tree,
                      'verdict': 'pass' if run.passed else 'fail', 'gate': gate_box.encode_run(run)}), flush=True)


if __name__ == '__main__':
    if sys.argv[2:] == ['--run-copy']:
        run_copy(Path(sys.argv[1]))
    else:
        raise SystemExit(run_guest(Path(sys.argv[1])))

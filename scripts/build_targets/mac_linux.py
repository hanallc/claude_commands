"""Run the gate in the Mac's Linux guest."""
from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext, redirect_stdout
from dataclasses import dataclass
from enum import StrEnum
import json
import math
from pathlib import Path
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
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
CLEANUP_SECONDS = 20


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
    """Keep the reservation around the guest attempt and its cleanup.

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


@dataclass(frozen=True)
class CompressedSourceCopy:
    """Hold the bootstrap frame and the Git archive frame for one SSH copy."""

    path: Path
    bootstrap_bytes: int
    compressed_bytes: int


def write_payload(repo: Path, folder: Path, request: GuestRequest, level: int) -> CompressedSourceCopy:
    request_file = folder / 'request.json'
    _ = request_file.write_text(json.dumps(request), encoding='utf-8')
    payload = folder / 'guest.tar.zst'
    with tempfile.TemporaryFile() as bootstrap, payload.open('wb') as output:
        with tarfile.open(fileobj=bootstrap, mode='w') as stream:
            stream.add(request_file, arcname='request.json')
            stream.add(Path(__file__).resolve(), arcname='runner.py')
        _ = bootstrap.seek(0)
        _ = subprocess.run(['zstd', f'-{level}', '-q', '-c'], stdin=bootstrap, stdout=output, check=True)
        bootstrap_bytes = os.fstat(output.fileno()).st_size
        archive = subprocess.run(
            ['bash', '-o', 'pipefail', '-c', 'git archive --format=tar "$1" | zstd "$2" -T0 -q -c',
             TARGET, request['commit'], f'-{level}'],
            cwd=repo, stdout=output, stderr=subprocess.PIPE, text=True, check=False)
        if archive.returncode:
            raise GuestUnavailable(f'source compression failed: {archive.stderr.strip() or archive.returncode}')
    return CompressedSourceCopy(payload, bootstrap_bytes, payload.stat().st_size - bootstrap_bytes)


def guest_shell(settings: GuestSettings, name: str) -> str:
    return f'''set -euo pipefail
if [ "$(id -un)" != {shlex.quote(settings.guest_user)} ]; then
    printf 'Expected SSH account %s.\\n' {shlex.quote(settings.guest_user)} >&2
    exit 1
fi
run="$HOME"/{shlex.quote(name)}'''


def run_shell(settings: GuestSettings, name: str, source: CandidateSource, bootstrap_bytes: int) -> str:
    return guest_shell(settings, name) + f'''
umask 077
mkdir -- "$run"
trap 'rm -rf -- "$run" || exit 1' EXIT
cd "$run"
exec 9> .running
flock -x 9
mkdir repo home home/.claude runtime bin
head -c {bootstrap_bytes} | zstd -d -q | tar -xf -
zstd -d -q | tar -xf - -C repo
ln -s "$run/repo/config" home/.claude/config
browser=$(command -v chromium) || {{ printf 'Chromium is absent from the guest PATH.\\n' >&2; exit 127; }}
ln -s "$browser" bin/google-chrome
export RUSTUP_HOME="${{RUSTUP_HOME:-$HOME/.rustup}}"
export HOME="$run/home" XDG_RUNTIME_DIR="$run/runtime" PYTHONUNBUFFERED=1
export USER={shlex.quote(settings.guest_user)} LOGNAME={shlex.quote(settings.guest_user)}
export USERNAME="$USER" LNAME="$USER" PATH="$run/bin:$PATH"
unset DBUS_SESSION_BUS_ADDRESS SSH_AUTH_SOCK SSH_AGENT_PID SUDO_USER SUDO_UID SUDO_GID
unset XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME XDG_STATE_HOME
unset GH_CONFIG_DIR GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN
unset CLAUDE_CONFIG_DIR CODEX_HOME CONF_DIR PYTHONPATH PYTHONHOME
git -C repo init -q --initial-branch=main
git -C repo -c core.autocrlf=false -c core.filemode=true add --force --all
tree=$(git -C repo write-tree)
if [ "$tree" != {shlex.quote(source.tree)} ]; then
    printf 'The archive tree %s differs from the requested tree.\\n' "$tree" >&2
    exit 1
fi
commit=$(git -C repo -c user.name='Mac Linux gate' -c user.email='mac-linux@localhost' commit-tree "$tree" -m 'Mac Linux gate source')
git -C repo update-ref refs/heads/main "$commit"
git -C repo remote add origin {shlex.quote(source.origin_url)}
git -C repo update-ref {ORIGIN_MAIN} "$commit"
git -C repo symbolic-ref refs/remotes/origin/HEAD {ORIGIN_MAIN}
umask 022
cd repo
set -- ./scripts/lib/py -X cpu_count={settings.jobs} ../runner.py
if command -v dbus-run-session >/dev/null 2>&1; then
    dbus-run-session -- "$@"
else
    "$@"
fi'''


def cleanup_guest(settings: GuestSettings, name: str) -> str:
    """Remove an incomplete upload after the guest releases its run lock."""
    shell = guest_shell(settings, name) + '''
if [ -d "$run" ]; then
    flock -n -E 75 "$run/.running" rm -rf -- "$run"
fi'''
    try:
        reply = subprocess.run(settings.command(shell), stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, errors='replace', timeout=CLEANUP_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f'guest cleanup failed ({error})'
    if reply.returncode == 75:
        return 'the guest still owns its run folder'
    if reply.returncode:
        return f'guest cleanup failed ({reply.stderr.strip() or reply.returncode})'
    return ''


def run_remote(settings: GuestSettings, name: str, source: CandidateSource, copy: CompressedSourceCopy) -> str:
    timeout = settings.run_time_limit_seconds + TRANSFER_ALLOWANCE_SECONDS
    try:
        with copy.path.open('rb') as payload:
            reply = subprocess.run(settings.command(run_shell(settings, name, source, copy.bootstrap_bytes)), stdin=payload,
                                   capture_output=True, text=True, errors='replace', timeout=timeout, check=False)
        if reply.returncode:
            detail = ' '.join(reply.stderr.split())[-1200:]
            raise GuestUnavailable(f'SSH or guest runner exited {reply.returncode}: {detail or "no diagnostic"}')
    except (OSError, subprocess.TimeoutExpired, GuestUnavailable) as error:
        cleanup = cleanup_guest(settings, name)
        detail = f'SSH or guest run exceeded {timeout} seconds' if isinstance(error, subprocess.TimeoutExpired) else str(error)
        raise GuestUnavailable(f'{detail}. {cleanup}' if cleanup else detail) from error
    return reply.stdout


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
    from scripts.production import affected_modules, gate

    try:
        registry = conf.read('build_targets')
        if registry.value(f'enabled.{TARGET}', GuestSwitch, recover_invalid=False) is GuestSwitch.OFF:
            return f'{TARGET} is off (build_targets.conf enabled.{TARGET}=off)'
        settings = read_settings(registry)
        if not math.isfinite(module_time_limit_s) or module_time_limit_s <= 0:
            raise GuestUnavailable('the module time limit must be positive and finite')
        repo = repo.resolve()
        source = candidate_source(repo, selected)
        attempt = uuid4().hex
        request = GuestRequest(attempt=attempt, commit=source.commit, tree=source.tree, mode=mode.wire, full=full,
                               selection=list(selected.paths) if isinstance(selected, TreeSelection) else 'all',
                               module_time_limit_s=module_time_limit_s, run_time_limit_seconds=settings.run_time_limit_seconds,
                               jobs=settings.jobs)
        with tempfile.TemporaryDirectory(prefix=f'{TARGET}-{attempt}-') as temporary:
            folder = Path(temporary)
            copy = write_payload(repo, folder, request, settings.zstd_level)
            with mac_linux_reservation(repo, mode, gate_job):
                reply = run_remote(settings, f'{TARGET}-{attempt}', source, copy)
                run = completed_run(reply, request, source, copy.compressed_bytes)
        if gate.checked_git(repo, 'rev-parse', 'HEAD') != source.head or cloud_gate.working_tree(repo)[0] != source.tree:
            raise GuestUnavailable('the checkout changed during the guest run')
        return run
    except (GuestUnavailable, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError,
            GateRefused, affected_modules.GraphError) as error:
        return f'{TARGET}: {error}'


def run_guest() -> None:
    """Run the candidate's gate and write one JSON result to standard output."""
    from scripts.production import gate
    from scripts.production.gate_routes import ALL_MODULES, SelectedModules
    from scripts.test_times.gate_record import GateTimeLimit

    repo = Path.cwd()
    request = cast(GuestRequest, json.loads((repo.parent / 'request.json').read_text(encoding='utf-8')))
    if os.cpu_count() != request['jobs']:
        raise GuestUnavailable(f'The guest Python did not set the CPU count to {request["jobs"]}.')
    tree = gate.checked_git(repo, 'rev-parse', 'HEAD^{tree}')
    if tree != request['tree']:
        raise GuestUnavailable('The guest checkout differs from the request.')
    selection = request['selection']
    with redirect_stdout(sys.stderr):
        run = gate.run_suites(repo, full=request['full'], mode=SuiteMode(request['mode']), machine=gate.WholeMachine(),
                              selected=ALL_MODULES if selection == 'all' else SelectedModules(tuple(selection)),
                              module_time_limit_s=request['module_time_limit_s'],
                              time_limit=GateTimeLimit(request['run_time_limit_seconds']))
    print(json.dumps({'attempt': request['attempt'], 'commit': request['commit'], 'tree': tree,
                      'verdict': 'pass' if run.passed else 'fail', 'gate': gate_box.encode_run(run)}), flush=True)


if __name__ == '__main__':
    run_guest()

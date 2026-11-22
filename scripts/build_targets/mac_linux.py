"""Run the gate in the Mac's Linux guest."""
from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext, redirect_stdout
from dataclasses import dataclass, replace
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
from typing import BinaryIO, Literal, TypedDict, cast
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
    ssh_path: tuple[str, str]
    account: str
    jobs: int
    run_time_limit_seconds: int

    def command(self, shell: str) -> tuple[str, ...]:
        mac, guest = self.ssh_path
        return (*SSH, mac, shlex.join((*SSH, guest, shlex.join(('bash', '-c', shell)))))


def read_settings(registry: conf.ConfigRead) -> GuestSettings:
    if (registry.text(f'kind.{TARGET}', recover_invalid=False) != 'ssh'
            or registry.text(f'platform.{TARGET}', recover_invalid=False) != 'linux'):
        raise GuestUnavailable(f'{TARGET} must use SSH and the Linux platform')
    settings_file = Path(registry.text(f'settings.{TARGET}', recover_invalid=False)).expanduser()
    settings = conf.read(registry.source.parent / settings_file)
    ssh_path = settings.value('ssh_path', str.split, recover_invalid=False)
    account = settings.text('account', recover_invalid=False)
    jobs = settings.value('jobs', int, recover_invalid=False)
    limit = settings.value('run_time_limit_seconds', int, recover_invalid=False)
    if len(ssh_path) != 2 or not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', host) for host in ssh_path):
        raise GuestUnavailable('ssh_path must name two SSH aliases')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', account):
        raise GuestUnavailable('account must name a Linux user')
    if jobs < 1 or limit < 1:
        raise GuestUnavailable('jobs and run_time_limit_seconds must be positive')
    return GuestSettings((ssh_path[0], ssh_path[1]), account, jobs, limit)


def mac_linux_reservation(_repo: Path, _sidecar: Path, _mode: SuiteMode,
                          _gate_job: StepJob | GateJobNotRecorded) -> AbstractContextManager[None]:
    """Keep the reservation around the guest attempt and its cleanup."""
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
    origin_main: str
    origin_url: str
    modules: frozenset[str]
    coverage: ModuleCoverage


def candidate_source(repo: Path, selected: AllModules | TreeSelection) -> CandidateSource:
    from scripts.aws_target import cloud_gate
    from scripts.production.gate import checked_git

    if checked_git(repo, 'rev-parse', '--is-shallow-repository') != 'false':
        raise GuestUnavailable('the source checkout needs full Git history')
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
    origin_main = checked_git(repo, 'rev-parse', ORIGIN_MAIN)
    origin_url = checked_git(repo, 'remote', 'get-url', 'origin')
    origin = urlsplit(origin_url)
    if origin.password or origin.query or ('://' in origin_url and origin.username):
        raise GuestUnavailable('the origin URL must contain no credential')
    commit = checked_git(repo, 'commit-tree', tree, '-p', head, '-m', 'Mac Linux gate snapshot') if dirty else head
    coverage = ModuleCoverage.COMPLETE if modules == paths else ModuleCoverage.SUBSET
    return CandidateSource(head, commit, tree, origin_main, origin_url, modules, coverage)


def write_payload(repo: Path, folder: Path, source: CandidateSource, request: GuestRequest) -> Path:
    from scripts.aws_target.cloud_gate import create_source_bundle

    bundle = folder / 'source.bundle'
    create_source_bundle(repo, bundle, (source.commit, source.origin_main))
    request_file = folder / 'request.json'
    _ = request_file.write_text(json.dumps(request), encoding='utf-8')
    payload = folder / 'guest.tar'
    with tarfile.open(payload, 'w') as stream:
        stream.add(bundle, arcname='source.bundle')
        stream.add(request_file, arcname='request.json')
        stream.add(Path(__file__).resolve(), arcname='runner.py')
    return payload


def guest_shell(settings: GuestSettings, name: str) -> str:
    return f'''set -eu
if [ "$(id -un)" != {shlex.quote(settings.account)} ]; then
    printf 'Expected SSH account %s.\\n' {shlex.quote(settings.account)} >&2
    exit 1
fi
run="$HOME"/{shlex.quote(name)}'''


def run_shell(settings: GuestSettings, name: str, source: CandidateSource) -> str:
    return guest_shell(settings, name) + f'''
umask 077
mkdir -- "$run"
trap 'rm -rf -- "$run" || exit 1' EXIT
cd "$run"
exec 9> .running
flock -x 9
tar -xf -
mkdir repo home home/.claude runtime bin
ln -s "$run/repo/config" home/.claude/config
git -C repo init -q --initial-branch=main
git -C repo bundle unbundle ../source.bundle >&2
git -C repo update-ref refs/heads/main {shlex.quote(source.origin_main)}
git -C repo remote add origin {shlex.quote(source.origin_url)}
git -C repo update-ref {ORIGIN_MAIN} {shlex.quote(source.origin_main)}
git -C repo symbolic-ref refs/remotes/origin/HEAD {ORIGIN_MAIN}
git -C repo checkout -q --detach {shlex.quote(source.commit)}
rm -- source.bundle
browser=$(command -v chromium) || {{ printf 'Chromium is absent from the guest PATH.\\n' >&2; exit 127; }}
ln -s "$browser" bin/google-chrome
export RUSTUP_HOME="${{RUSTUP_HOME:-$HOME/.rustup}}"
export HOME="$run/home" XDG_RUNTIME_DIR="$run/runtime" PYTHONUNBUFFERED=1
export USER={shlex.quote(settings.account)} LOGNAME={shlex.quote(settings.account)}
export USERNAME="$USER" LNAME="$USER" PATH="$run/bin:$PATH"
unset DBUS_SESSION_BUS_ADDRESS SSH_AUTH_SOCK SSH_AGENT_PID SUDO_USER SUDO_UID SUDO_GID
unset XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME XDG_STATE_HOME
unset GH_CONFIG_DIR GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN
unset CLAUDE_CONFIG_DIR CODEX_HOME CONF_DIR PYTHONPATH PYTHONHOME
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


def run_remote(settings: GuestSettings, name: str, source: CandidateSource, payload: BinaryIO) -> str:
    timeout = settings.run_time_limit_seconds + TRANSFER_ALLOWANCE_SECONDS
    try:
        reply = subprocess.run(settings.command(run_shell(settings, name, source)), stdin=payload,
                               capture_output=True, text=True, errors='replace', timeout=timeout, check=False)
        if reply.returncode:
            detail = ' '.join(reply.stderr.split())[-1200:]
            raise GuestUnavailable(f'SSH or guest runner exited {reply.returncode}: {detail or "no diagnostic"}')
    except (OSError, subprocess.TimeoutExpired, GuestUnavailable) as error:
        cleanup = cleanup_guest(settings, name)
        detail = f'SSH or guest run exceeded {timeout} seconds' if isinstance(error, subprocess.TimeoutExpired) else str(error)
        raise GuestUnavailable(f'{detail}. {cleanup}' if cleanup else detail) from error
    return reply.stdout


def completed_run(text: str, request: GuestRequest, source: CandidateSource) -> GateRun:
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
    return replace(run, coverage=source.coverage)


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
            payload = write_payload(repo, folder, source, request)
            with mac_linux_reservation(repo, folder / 'reservation.cgroup', mode, gate_job):
                with payload.open('rb') as stream:
                    run = completed_run(run_remote(settings, f'{TARGET}-{attempt}', source, stream), request, source)
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
    commit = gate.checked_git(repo, 'rev-parse', 'HEAD')
    tree = gate.checked_git(repo, 'rev-parse', 'HEAD^{tree}')
    if (commit, tree) != (request['commit'], request['tree']):
        raise GuestUnavailable('The guest checkout differs from the request.')
    selection = request['selection']
    with redirect_stdout(sys.stderr):
        run = gate.run_suites(repo, full=request['full'], mode=SuiteMode(request['mode']), machine=gate.WholeMachine(),
                              selected=ALL_MODULES if selection == 'all' else SelectedModules(tuple(selection)),
                              module_time_limit_s=request['module_time_limit_s'],
                              time_limit=GateTimeLimit(request['run_time_limit_seconds']))
    print(json.dumps({'attempt': request['attempt'], 'commit': commit, 'tree': tree,
                      'verdict': 'pass' if run.passed else 'fail', 'gate': gate_box.encode_run(run)}), flush=True)


if __name__ == '__main__':
    run_guest()

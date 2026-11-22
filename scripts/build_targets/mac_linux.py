"""Run the gate in the Mac's Linux guest."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager, nullcontext, redirect_stdout
from dataclasses import dataclass
from enum import StrEnum
import fcntl
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
from scripts.lib import build_folders, conf
from scripts.production.gate_modules import GateRefused, discover_test_modules
from scripts.production.gate_routes import AllModules, GateJobKind, GateRun, ModuleCoverage, SuiteMode, TreeSelection


TARGET = 'mac-linux'
ORIGIN_MAIN = 'refs/remotes/origin/main'
SSH = ('ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ForwardAgent=no',
       '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2')
# Give the transfer, checkout, and result time outside the guest runner deadline.
TRANSFER_ALLOWANCE_SECONDS = 120
GUEST_REF_TIMEOUT_SECONDS = 30
GUEST_KILL_AFTER_SECONDS = 3
GUEST_CLEANUP_LOCK_SECONDS = 5
GUEST_CLEANUP_TIMEOUT_SECONDS = 25
MISSING_PREREQUISITE_STATUS = 75
MISSING_PREREQUISITE_MESSAGE = 'Repository lacks these prerequisite commits:'


class GuestUnavailable(RuntimeError):
    """The guest cannot return a completed gate."""


class GuestMissingPrerequisite(GuestUnavailable):
    """The guest needs the full Git bundle."""


class GuestRepositoryRefused(GuestUnavailable):
    """The source repository cannot use the Mac Linux route."""

    def __init__(self, repository: str) -> None:
        self.repository: str = repository
        super().__init__(f'the Mac Linux guest accepts claude_commands, but the source repository is {repository}')


class GuestSwitch(StrEnum):
    ON = 'on'
    OFF = 'off'


@dataclass(frozen=True)
class GuestSettings:
    ssh_hops: tuple[str, str]
    guest_user: str
    jobs: int
    run_time_limit_seconds: int

    @property
    def transfer_timeout_seconds(self) -> int:
        return self.run_time_limit_seconds + TRANSFER_ALLOWANCE_SECONDS

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
    if len(hops) != 2 or not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', host) for host in hops):
        raise GuestUnavailable('ssh.hops must name two SSH aliases')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', user):
        raise GuestUnavailable('guest.user must name a Linux user')
    if jobs < 1 or limit < 1:
        raise GuestUnavailable('test.jobs and run.time_limit_seconds must be positive')
    return GuestSettings((hops[0], hops[1]), user, jobs, limit)


def mac_linux_reservation(_repo: Path, _work: SuiteMode | StepJob,
                          _gate_job: StepJob | GateJobNotRecorded) -> AbstractContextManager[None]:
    """Keep the reservation through the guest attempt.

    B3 adds scripts.build_queue.mac_run.held(repo, 'mac', work, gate_job).
    The context manager takes the native claude_commands repository claim on the Mac.
    Set the integer status field on the value that it yields.
    Return str(error) as the route fallback on mac_run.MacRunRefused.
    """
    # TODO(item 191 B3): Reserve mac-linux through the queue before the guest attempt.
    return nullcontext()


class GuestRequest(TypedDict):
    attempt: str
    commit: str
    tree: str
    origin_main: str
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
    origin_main: str
    origin_url: str
    modules: frozenset[str]
    coverage: ModuleCoverage


def candidate_source(repo: Path, selected: AllModules | TreeSelection) -> CandidateSource:
    from scripts.aws_target import cloud_gate
    from scripts.production.gate import checked_git

    repository = build_folders.repo_word(repo)
    if repository != 'claude':
        raise GuestRepositoryRefused(repository)
    head = checked_git(repo, 'rev-parse', 'HEAD')
    origin_main = checked_git(repo, 'rev-parse', '--verify', f'{ORIGIN_MAIN}^{{commit}}')
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
    return CandidateSource(head, commit, tree, origin_main, origin_url, modules, coverage)


def runner_bootstrap(request: GuestRequest) -> bytes:
    manifest = json.dumps(request).encode('utf-8')
    record = tarfile.TarInfo('request.json')
    record.size = len(manifest)
    with BytesIO() as bootstrap:
        with tarfile.open(fileobj=bootstrap, mode='w') as archive:
            archive.addfile(record, BytesIO(manifest))
            archive.add(Path(__file__).resolve(), arcname='runner.py')
        return bootstrap.getvalue()


def guest_shell(settings: GuestSettings) -> str:
    return f'''set -euo pipefail
if [ "$(id -un)" != {shlex.quote(settings.guest_user)} ]; then
    printf 'Expected SSH account %s.\\n' {shlex.quote(settings.guest_user)} >&2
    exit 1
fi
checkout="$HOME/.claude"
if [ -L "$checkout" ] || [ -L "$checkout/.git" ]; then
    printf 'The guest main checkout must be a real directory.\\n' >&2
    exit 1
fi'''


@dataclass(frozen=True)
class EmptyGuestMain:
    """The guest does not have a main commit."""


@dataclass(frozen=True)
class GuestMain:
    """Use the guest main commit as a bundle prerequisite."""

    commit: str


def read_guest_main(settings: GuestSettings) -> GuestMain | EmptyGuestMain:
    shell = guest_shell(settings) + '''
if [ -f "$checkout/.git/HEAD" ]; then
    git -C "$checkout" rev-parse --verify --quiet refs/heads/main || test "$?" -eq 1
fi'''
    reply = subprocess.run(settings.command(shell), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           errors='replace', timeout=GUEST_REF_TIMEOUT_SECONDS, check=False)
    if reply.returncode:
        raise GuestUnavailable(f'guest main probe exited {reply.returncode}: {reply.stderr.strip() or "no diagnostic"}')
    commit = reply.stdout.strip()
    if not commit:
        return EmptyGuestMain()
    if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit) is None:
        raise GuestUnavailable('the guest main probe returned an invalid commit ID')
    return GuestMain(commit)


def guest_attempt_shell(settings: GuestSettings, attempt: str) -> str:
    return guest_shell(settings) + f'''
state="$checkout/.git/mac-linux-runs"/{shlex.quote(attempt)}'''


def run_shell(settings: GuestSettings, source: CandidateSource, bootstrap_bytes: int, attempt: str) -> str:
    object_format = 'sha256' if len(source.commit) == 64 else 'sha1'
    return guest_attempt_shell(settings, attempt) + f'''
umask 077
mkdir -p -- "$checkout"
cd "$checkout"
exec 9< .
flock -x -n 9 || {{ printf 'The guest main checkout is in use.\\n' >&2; exit 1; }}
if [ ! -f .git/mac-linux-owned ]; then
    if [ -n "$(find . -mindepth 1 -maxdepth 1 ! -name .git -print -quit)" ] ||
       {{ [ -f .git/HEAD ] && [ -n "$(git for-each-ref --count=1)" ]; }}; then
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
if [ ! -f .git/HEAD ]; then
    git init -q --initial-branch=main --object-format={object_format}
fi
mkdir -p -- "$state"
if [ -e "$state/cancelled" ]; then
    rm -rf -- "$state"
    printf 'The caller cancelled the guest run.\\n' >&2
    exit 1
fi
bootstrap="$checkout/.git/mac-linux"
rm -rf -- "$bootstrap"
mkdir -p -- "$bootstrap/tmp"
trap 'rm -rf -- "$bootstrap"; touch "$state/finished" 2>/dev/null || :' EXIT
export TMPDIR="$bootstrap/tmp" XDG_CACHE_HOME="$bootstrap/cache"
head -c {bootstrap_bytes} | tar -xf - -C "$bootstrap"
if ! LC_ALL=C git bundle unbundle - >/dev/null 2>"$bootstrap/bundle-error"; then
    cat "$bootstrap/bundle-error" >&2
    if grep -Fq {shlex.quote(MISSING_PREREQUISITE_MESSAGE)} "$bootstrap/bundle-error"; then
        rm -rf -- "$state"
        exit {MISSING_PREREQUISITE_STATUS}
    fi
    exit 1
fi
commit={shlex.quote(source.commit)}
origin_main={shlex.quote(source.origin_main)}
tree=$(git rev-parse --verify "$commit^{{tree}}")
if [ "$tree" != {shlex.quote(source.tree)} ]; then
    printf 'The bundle tree %s differs from the requested tree.\\n' "$tree" >&2
    exit 1
fi
git cat-file -e "$origin_main^{{commit}}"
git config remote.origin.url {shlex.quote(source.origin_url)}
git symbolic-ref HEAD refs/heads/main
git symbolic-ref refs/remotes/origin/HEAD {ORIGIN_MAIN}
printf 'update refs/heads/main %s\\nupdate {ORIGIN_MAIN} %s\\n' "$origin_main" "$origin_main" | git update-ref --stdin
git reset --hard -q "$origin_main"
umask 022
./scripts/lib/py "$bootstrap/runner.py" "$bootstrap/request.json" "$state"
rm -rf -- "$state"'''


def cleanup_shell(settings: GuestSettings, attempt: str) -> str:
    return guest_attempt_shell(settings, attempt) + f'''
umask 077
mkdir -p -- "$state"
exec 8< "$state"
flock -x -w {GUEST_CLEANUP_LOCK_SECONDS} 8
touch -- "$state/cancelled"
if [ ! -f "$state/copy" ]; then
    printf 'no numbered copy was registered\\n'
    if [ -e "$state/finished" ]; then
        rm -rf -- "$state"
    fi
    exit 0
fi
mapfile -d '' -t paths < "$state/copy"
group_file="${{paths[0]}}"
lock_file="${{paths[1]}}"
stopped=''
if [ -f "$group_file" ] && read -r group started < "$group_file"; then
    if [[ ! "$group" =~ ^[1-9][0-9]*$ || "$group" -le 1 || ! "$started" =~ ^[0-9]+$ ]]; then
        printf 'The guest process group record is invalid.\\n' >&2
        exit 1
    fi
    if stat=$(cat "/proc/$group/stat" 2>/dev/null); then
        read -r -a fields <<< "${{stat##*) }}"
        if [[ "${{fields[2]}}" = "$group" && "${{fields[3]}}" = "$group" &&
              "${{fields[19]}}" = "$started" && "${{fields[0]}}" != Z ]]; then
            if kill -KILL -- "-$group" 2>/dev/null; then
                stopped="stopped guest process group $group; "
            fi
        fi
    fi
fi
flock -u 8
exec 7< "$lock_file"
if ! flock -x -w {GUEST_CLEANUP_LOCK_SECONDS} 7; then
    printf '%snumbered copy lock is still held\\n' "$stopped" >&2
    exit 1
fi
if [ -L "$lock_file" ] || ! [ "$lock_file" -ef "/proc/$$/fd/7" ]; then
    printf '%snumbered copy lock changed\\n' "$stopped" >&2
    exit 1
fi
printf '%snumbered copy lock is free\\n' "$stopped"
rm -rf -- "$state"'''


def failed_guest_run(settings: GuestSettings, attempt: str, detail: str) -> GuestUnavailable:
    try:
        reply = subprocess.run(settings.command(cleanup_shell(settings, attempt)), stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, errors='replace',
                               timeout=GUEST_CLEANUP_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return GuestUnavailable(f'{detail}. Guest cleanup unconfirmed: {error}')
    if reply.returncode:
        reason = reply.stderr.strip() or f'SSH exited {reply.returncode}'
        return GuestUnavailable(f'{detail}. Guest cleanup unconfirmed: {reason}')
    return GuestUnavailable(f'{detail}. Guest cleanup: {reply.stdout.strip()}')


class BundleForm(StrEnum):
    FIRST = 'first'
    DELTA = 'delta'


class BundleReason(StrEnum):
    FIRST_RUN = 'the guest has no main commit'
    SOURCE_LACKS_BASE = 'the guest main commit is absent from the source repository'
    GUEST_LACKS_PREREQUISITE = 'the guest lacks a Git bundle prerequisite'
    DELTA = 'the guest accepted the delta bundle'


@dataclass(frozen=True)
class FullBundle:
    """Send the complete Git history for the candidate and origin/main."""

    reason: BundleReason


@dataclass(frozen=True)
class DeltaBundle:
    """Exclude the Git history that the guest main already has."""

    base: str


@dataclass(frozen=True)
class GuestReply:
    result: str
    bundle_bytes: int
    bundle_form: BundleForm
    bundle_reason: BundleReason


@contextmanager
def bundle_refs(repo: Path, source: CandidateSource, attempt: str) -> Generator[str]:
    """Keep named bundle tips for the duration of the transfer."""
    from scripts.production.gate import checked_git

    prefix = f'refs/mac-linux/{attempt}'
    try:
        _ = checked_git(repo, 'update-ref', f'{prefix}/candidate', source.commit)
        _ = checked_git(repo, 'update-ref', f'{prefix}/main', source.origin_main)
        yield prefix
    finally:
        _ = checked_git(repo, 'update-ref', '-d', f'{prefix}/candidate')
        _ = checked_git(repo, 'update-ref', '-d', f'{prefix}/main')


def empty_delta_header(repo: Path, source: CandidateSource, basis: DeltaBundle, prefix: str) -> str:
    from scripts.production.gate import checked_git

    if checked_git(repo, 'rev-list', '--max-count=1', source.commit, source.origin_main, f'^{basis.base}'):
        return ''
    object_format = 'sha256' if len(source.commit) == 64 else 'sha1'
    # Git refuses to create a bundle when both tips are already in the prerequisite.
    return (f'# v3 git bundle\n@object-format={object_format}\n-{basis.base} guest main\n'
            + f'{source.commit} {prefix}/candidate\n{source.origin_main} {prefix}/main\n\n')


# Send the SSH result to descriptor 3. Use descriptor 4 to collect the byte count.
STREAM_SOURCE = '''
prefix=$1
base=$2
header=$3
shift 3
bundle() {
    if [ -n "$header" ]; then
        printf '%s' "$header" && git pack-objects --stdout </dev/null
    else
        local refs=("$prefix/candidate" "$prefix/main")
        if [ -n "$base" ]; then
            refs+=("^$base")
        fi
        git bundle create - "${refs[@]}"
    fi
}
exec 3>&1
size=$(
    exec 4>&1
    { cat && bundle; } | tee >(wc -c >&4) | "$@" >&3 3>&- 4>&-
) || exit "$?"
printf '\\n%s\\n' "$size"
'''


def run_remote(repo: Path, settings: GuestSettings, source: CandidateSource, request: GuestRequest,
               prefix: str, bundle: FullBundle | DeltaBundle) -> GuestReply:
    bootstrap = runner_bootstrap(request)
    if isinstance(bundle, DeltaBundle):
        base = bundle.base
        header = empty_delta_header(repo, source, bundle, prefix)
        form, reason = BundleForm.DELTA, BundleReason.DELTA
    else:
        base, header = '', ''
        form, reason = BundleForm.FIRST, bundle.reason
    timeout = settings.transfer_timeout_seconds
    transport = uuid4().hex
    command = settings.command(run_shell(settings, source, len(bootstrap), transport))
    try:
        with subprocess.Popen(['bash', '-o', 'pipefail', '-c', STREAM_SOURCE, TARGET, prefix, base, header,
                               *command], cwd=repo, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as stream:
            try:
                output, errors = stream.communicate(bootstrap, timeout=timeout)
            except BaseException:
                try:
                    os.killpg(stream.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise
    except subprocess.TimeoutExpired as error:
        raise failed_guest_run(settings, transport, f'SSH or guest run exceeded {timeout} seconds') from error
    if stream.returncode:
        diagnostics = errors.decode(errors='replace')
        if stream.returncode == MISSING_PREREQUISITE_STATUS and MISSING_PREREQUISITE_MESSAGE in diagnostics:
            raise GuestMissingPrerequisite(diagnostics.strip())
        detail = ' '.join(diagnostics.split())[-1200:]
        raise failed_guest_run(settings, transport, f'SSH or source stream exited {stream.returncode}: {detail or "no diagnostic"}')
    result, _, count = output.rstrip().rpartition(b'\n')
    return GuestReply(result.decode('utf-8'), int(count) - len(bootstrap), form, reason)


def transfer_source(repo: Path, settings: GuestSettings, source: CandidateSource, request: GuestRequest) -> GuestReply:
    guest = read_guest_main(settings)
    with bundle_refs(repo, source, request['attempt']) as prefix:
        if isinstance(guest, EmptyGuestMain):
            reason = BundleReason.FIRST_RUN
        elif subprocess.run(['git', 'cat-file', '-e', f'{guest.commit}^{{commit}}'], cwd=repo,
                            capture_output=True, timeout=TRANSFER_ALLOWANCE_SECONDS, check=False).returncode:
            reason = BundleReason.SOURCE_LACKS_BASE
        else:
            try:
                return run_remote(repo, settings, source, request, prefix, DeltaBundle(guest.commit))
            except GuestMissingPrerequisite:
                reason = BundleReason.GUEST_LACKS_PREREQUISITE
        return run_remote(repo, settings, source, request, prefix, FullBundle(reason))


@dataclass(frozen=True, kw_only=True)
class MacLinuxGateRun(GateRun):
    """Keep the bundle details and the skip counts from the guest."""

    bundle_bytes: int
    bundle_form: BundleForm
    bundle_reason: BundleReason
    module_skips: dict[str, int]


def completed_run(reply: GuestReply, request: GuestRequest, source: CandidateSource) -> MacLinuxGateRun:
    row = ledger.mapping(cast(object, json.loads(reply.result)))
    for key in ('attempt', 'commit', 'tree', 'origin_main'):
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
                           bundle_bytes=reply.bundle_bytes, bundle_form=reply.bundle_form,
                           bundle_reason=reply.bundle_reason, module_skips=skips)


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
        request = GuestRequest(attempt=uuid4().hex, commit=source.commit, tree=source.tree,
                               origin_main=source.origin_main, mode=mode.wire, full=full,
                               selection=list(selected.paths) if isinstance(selected, TreeSelection) else 'all',
                               module_time_limit_s=module_time_limit_s, run_time_limit_seconds=settings.run_time_limit_seconds,
                               jobs=settings.jobs, unit=unit)
        with mac_linux_reservation(repo, mode, gate_job):
            reply = transfer_source(repo, settings, source, request)
            run = completed_run(reply, request, source)
        if gate.checked_git(repo, 'rev-parse', 'HEAD') != source.head or cloud_gate.working_tree(repo)[0] != source.tree:
            raise GuestUnavailable('the checkout changed during the guest run')
        return run
    except (GuestUnavailable, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError,
            GateRefused, affected_modules.GraphError) as error:
        return f'{TARGET}: {error}'


def run_guest(request_path: Path, state: Path) -> int:
    """Take a numbered copy before starting the guest gate."""
    from scripts.lib import build_slots

    request = guest_request(request_path)
    main = Path.cwd()
    with build_slots.take_copy(main, request['commit'], showrunner='', unit=request['unit'],
                               work=request['mode'], purpose=TARGET) as copy:
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
            command[:0] = ['timeout', f'--kill-after={GUEST_KILL_AFTER_SECONDS}', str(request['run_time_limit_seconds'])]
            return run_guest_group(command, state, runtime, copy.path, environment)


def run_guest_group(command: list[str], state: Path, runtime: Path, copy: Path, environment: dict[str, str]) -> int:
    from scripts.lib.process_start import Started, Zombie, proc_table_start

    guard = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if (state / 'cancelled').exists():
            raise GuestUnavailable('The caller cancelled the guest run.')
        group_file = runtime / f'{state.name}.group'
        lock_file = copy.with_name(copy.name + '.lock')
        _ = (state / 'copy').write_bytes(os.fsencode(group_file) + b'\0' + os.fsencode(lock_file) + b'\0')
        with subprocess.Popen(command, cwd=copy, env=environment, start_new_session=True,
                              pass_fds=build_folders.held_build_folder_descriptors(), stdin=subprocess.DEVNULL) as child:
            try:
                start = proc_table_start(child.pid, Path('/proc'))
                if not isinstance(start, (Started, Zombie)):
                    raise GuestUnavailable('The guest process group identity is unavailable.')
                _ = group_file.write_text(f'{child.pid} {start.start}\n', encoding='ascii')
            except BaseException:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise
            finally:
                fcntl.flock(guard, fcntl.LOCK_UN)
            return child.wait()
    finally:
        os.close(guard)


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
    commit = gate.checked_git(repo, 'rev-parse', 'HEAD')
    origin_main = gate.checked_git(repo, 'rev-parse', ORIGIN_MAIN)
    if commit != request['commit'] or origin_main != request['origin_main']:
        raise GuestUnavailable('The guest checkout refs differ from the request.')
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
    print(json.dumps({'attempt': request['attempt'], 'commit': commit, 'tree': tree, 'origin_main': origin_main,
                      'verdict': 'pass' if run.passed else 'fail', 'gate': gate_box.encode_run(run)}), flush=True)


if __name__ == '__main__':
    if sys.argv[2:] == ['--run-copy']:
        run_copy(Path(sys.argv[1]))
    else:
        raise SystemExit(run_guest(Path(sys.argv[1]), Path(sys.argv[2])))

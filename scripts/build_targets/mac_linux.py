"""Run the gate in the Mac's Linux guest.

Wait for proof that the guest run ended.
If the wait expires, fail the route. The guest keeps its run lock until the run ends.
If the gate parent and module supervisor die, a descendant can keep the lock and prevent proof.
Without a signal handler, Python can raise KeyboardInterrupt at any bytecode. An interrupt outside the protected block acts like process death. The guest timeout still ends the run.
"""
from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext, redirect_stdout, suppress
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
from time import monotonic, sleep, time
from typing import Literal, TypedDict, cast
from urllib.parse import urlsplit
from uuid import uuid4

from scripts.aws_target import gate_box, ledger
from scripts.build_queue.admission_jobs import GateJobNotRecorded, StepJob
from scripts.build_targets import target_config
from scripts.lib import build_folders, conf, process_group
from scripts.mac_test import eligibility
from scripts.production.gate_modules import GateRefused, discover_test_modules
from scripts.production.gate_routes import AllModules, GateJobKind, GateRun, ModuleCoverage, SuiteMode, TreeSelection
from scripts.test_times.gate_record import TYPE_CHECK_JOB


TARGET = eligibility.MAC_LINUX_TARGET
ORIGIN_MAIN = 'refs/remotes/origin/main'
SSH_CONNECT_TIMEOUT_SECONDS = 8
SSH = ('ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ForwardAgent=no',
       '-o', f'ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2')
# Allow time for setup, cleanup, and result transfer.
TRANSFER_ALLOWANCE_SECONDS = 120
GUEST_REF_TIMEOUT_SECONDS = 30
GUEST_KILL_GRACE_SECONDS = 3
GUEST_CLEANUP_TIMEOUT_SECONDS = 25
MISSING_PREREQUISITE_STATUS = 75
MISSING_PREREQUISITE_MESSAGE = 'Repository lacks these prerequisite commits:'
GUEST_END_PENDING_STATUS = 76
RUN_LOCK_PATH = '.git/mac-linux-run.lock'
RUN_LOCK_FD = 9
ARM_RECORD_PREFIX = 'mac-linux-armed'
CANCEL_RECORD_PREFIX = 'mac-linux-cancel'


class GuestUnavailable(RuntimeError):
    """The guest cannot return a completed gate."""


class GuestMissingPrerequisite(GuestUnavailable):
    """The guest needs the full Git bundle."""


class GuestRepositoryRefused(GuestUnavailable):
    """The source repository cannot use the Mac Linux route."""

    def __init__(self, repository: str) -> None:
        self.repository: str = repository
        super().__init__(f'the Mac Linux guest accepts claude_commands, but the source repository is {repository}')


@dataclass(frozen=True)
class GuestSettings:
    ssh_hops: tuple[str, str]
    guest_user: str
    jobs: int
    run_time_limit_seconds: int
    unreachable_wait_seconds: int

    @property
    def guest_timeout_seconds(self) -> int:
        return self.run_time_limit_seconds + TRANSFER_ALLOWANCE_SECONDS

    @property
    def transfer_timeout_seconds(self) -> int:
        return self.guest_timeout_seconds + TRANSFER_ALLOWANCE_SECONDS

    def command(self, shell: str) -> tuple[str, ...]:
        mac, guest = self.ssh_hops
        return (*SSH, mac, shlex.join((*SSH, '-l', self.guest_user, guest, shlex.join(('bash', '-c', shell)))))


def read_settings(registry: conf.ConfigRead) -> GuestSettings:
    settings = conf.read(eligibility.mac_linux_target(registry).settings)
    hops = settings.value('ssh.hops', str.split, recover_invalid=False)
    user = settings.text('guest.user', recover_invalid=False)
    jobs = settings.value('test.jobs', int, recover_invalid=False)
    limit = settings.value('run.time_limit_seconds', int, recover_invalid=False)
    proof_wait = target_config.unreachable_wait_seconds(registry)
    if len(hops) != 2 or not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', host) for host in hops):
        raise GuestUnavailable('ssh.hops must name two SSH aliases')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', user):
        raise GuestUnavailable('guest.user must name a Linux user')
    if jobs < 1 or limit < 1:
        raise GuestUnavailable('test.jobs and run.time_limit_seconds must be positive')
    return GuestSettings((hops[0], hops[1]), user, jobs, limit, proof_wait)


def mac_linux_reservation(_repo: Path, _work: SuiteMode | StepJob,
                          _gate_job: StepJob | GateJobNotRecorded) -> AbstractContextManager[None]:
    """Provide the reservation context that item 191 B3 will implement.

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
    type_dirs: list[str]
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


def guest_checkout_shell(settings: GuestSettings) -> str:
    return guest_shell(settings) + '''
umask 077
mkdir -p -- "$checkout/.git"
cd "$checkout"'''


@dataclass(frozen=True)
class EmptyGuestMain:
    """The guest does not have a main commit."""


@dataclass(frozen=True)
class GuestMain:
    """Use the guest main commit as a bundle prerequisite."""

    commit: str


def read_guest_main(settings: GuestSettings) -> GuestMain | EmptyGuestMain:
    shell = guest_shell(settings) + '''
if ! command -v bwrap >/dev/null 2>&1; then
    printf 'bwrap is absent from the guest PATH.\\n' >&2
    exit 127
fi
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


@dataclass(frozen=True)
class ArmedGuestAttempt:
    """Identify the run and its deadline on the guest."""

    attempt: str
    boot_id: str
    deadline_ns: int


ARM_ATTEMPT = '''\
import os
import sys
import time

prefix, attempt, window = sys.argv[1:]
if not attempt or any(character not in '0123456789abcdefghijklmnopqrstuvwxyz-' for character in attempt):
    sys.exit('The guest attempt ID is invalid.')
with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as identity:
    boot = identity.read().strip()
git = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
deadline = time.clock_gettime_ns(time.CLOCK_BOOTTIME) + int(window) * 10**9
record = f'{prefix}.{boot}.{deadline}.{attempt}'
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
os.close(os.open(record, flags, 0o600, dir_fd=git))
os.close(git)
print(f'armed {boot} {deadline}', flush=True)
'''


def stop_ssh_process(process: subprocess.Popen[bytes]) -> None:
    while True:
        try:
            with suppress(ProcessLookupError):
                process_group.send_process_group(process.pid, signal.SIGKILL)
            _ = process.wait()
            return
        except KeyboardInterrupt:
            continue


def arm_guest(settings: GuestSettings, attempt: str) -> ArmedGuestAttempt:
    arm = shlex.join(('python3', '-I', '-S', '-c', ARM_ATTEMPT, ARM_RECORD_PREFIX, attempt,
                      str(SSH_CONNECT_TIMEOUT_SECONDS)))
    command = settings.command(guest_checkout_shell(settings) + f'\nexec {arm}')
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True) as process:
        try:
            output, errors = process.communicate(timeout=GUEST_REF_TIMEOUT_SECONDS)
        except BaseException:
            stop_ssh_process(process)
            raise
    if process.returncode:
        diagnostic = errors.decode(errors='replace').strip() or 'no diagnostic'
        raise GuestUnavailable(f'guest arm exited {process.returncode}: {diagnostic}')
    reply = re.fullmatch(rb'armed ([0-9a-f-]{36}) ([0-9]{1,20})\n', output)
    if reply is None:
        raise GuestUnavailable('the guest did not acknowledge the armed deadline')
    return ArmedGuestAttempt(attempt, reply[1].decode('ascii'), int(reply[2]))


OPEN_RUN_LOCK = '''\
import os
import stat
import sys

path, number, shell = sys.argv[1:]
try:
    if os.path.islink(path):
        raise OSError('The guest run lock must not be a symlink.')
    directory, name = os.path.split(path)
    parent = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        lock = os.open(name, flags, 0o600, dir_fd=parent)
    finally:
        os.close(parent)
    if not stat.S_ISREG(os.fstat(lock).st_mode):
        raise OSError('The guest run lock must be a regular file.')
except OSError as error:
    sys.exit(str(error))
descriptor = int(number)
if lock == descriptor:
    os.set_inheritable(lock, True)
else:
    os.dup2(lock, descriptor)
    os.close(lock)
os.execvp('bash', ['bash', '-c', shell])
'''


def open_run_lock_command(descriptor: int, shell: str) -> str:
    return shlex.join(('python3', '-I', '-S', '-c', OPEN_RUN_LOCK, RUN_LOCK_PATH, str(descriptor), shell))


GUEST_ATTEMPT_RECORDS = '''\
import fcntl
import os
import stat
import sys
import time

mode, arm_prefix, cancel_prefix, attempt, boot, deadline, lock_name, descriptor, *arguments = sys.argv[1:]
with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as identity:
    boot_id = identity.read().strip()
git = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
arm = f'{arm_prefix}.{boot}.{deadline}.{attempt}'
cancel = f'{cancel_prefix}.{boot}.{deadline}.{attempt}'
lock = int(descriptor)
path_stat = os.stat(lock_name, dir_fd=git, follow_symlinks=False)
if not os.path.samestat(os.fstat(lock), path_stat):
    sys.exit('The guest run lock path changed.')
if mode == 'cancel':
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        os.close(os.open(cancel, flags, 0o600, dir_fd=git))
    except FileExistsError:
        if not stat.S_ISREG(os.stat(cancel, dir_fd=git, follow_symlinks=False).st_mode):
            sys.exit('The guest cancel record must be a regular file.')
    now = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    for name in os.listdir(git):
        fields = name.split('.')
        if len(fields) == 4 and fields[0] in (arm_prefix, cancel_prefix) and fields[2].isdecimal() and name != cancel:
            _, record_boot, record_deadline, _ = fields
            if record_boot != boot_id or now >= int(record_deadline):
                os.unlink(name, dir_fd=git)
    sys.exit(0)

if boot_id != boot or time.clock_gettime_ns(time.CLOCK_BOOTTIME) >= int(deadline):
    sys.exit('The guest armed deadline passed.')
try:
    if not stat.S_ISREG(os.stat(arm, dir_fd=git, follow_symlinks=False).st_mode):
        sys.exit('The guest armed record must be a regular file.')
except FileNotFoundError:
    sys.exit('The guest attempt has no armed record.')
try:
    os.stat(cancel, dir_fd=git, follow_symlinks=False)
except FileNotFoundError:
    pass
else:
    sys.exit('The guest attempt was cancelled.')
if mode == 'lock':
    if time.clock_gettime_ns(time.CLOCK_BOOTTIME) >= int(deadline):
        sys.exit('The guest armed deadline passed.')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit('The guest run lock is held.')
    if time.clock_gettime_ns(time.CLOCK_BOOTTIME) >= int(deadline):
        fcntl.flock(lock, fcntl.LOCK_UN)
        sys.exit('The guest armed deadline passed.')
    sys.exit(0)
os.close(git)
group, shell = arguments
os.ftruncate(lock, 0)
os.write(lock, f'{group} {attempt}\\n'.encode('ascii'))
if time.clock_gettime_ns(time.CLOCK_BOOTTIME) >= int(deadline):
    sys.exit('The guest armed deadline passed.')
os.execvp('bash', ['bash', '-c', shell])
'''


def guest_attempt_command(mode: Literal['lock', 'start', 'cancel'], armed: ArmedGuestAttempt, descriptor: int) -> str:
    return shlex.join(('python3', '-I', '-S', '-c', GUEST_ATTEMPT_RECORDS, mode, ARM_RECORD_PREFIX, CANCEL_RECORD_PREFIX,
                       armed.attempt, armed.boot_id, str(armed.deadline_ns), Path(RUN_LOCK_PATH).name, str(descriptor)))


def run_shell(settings: GuestSettings, source: CandidateSource, bootstrap_bytes: int, armed: ArmedGuestAttempt) -> str:
    object_format = 'sha256' if len(source.commit) == 64 else 'sha1'
    checkout = f'''set -euo pipefail
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
bootstrap="$PWD/.git/mac-linux"
rm -rf -- "$bootstrap"
mkdir -p -- "$bootstrap/tmp"
trap 'rm -rf -- "$bootstrap"' EXIT
export TMPDIR="$bootstrap/tmp" XDG_CACHE_HOME="$bootstrap/cache"
head -c {bootstrap_bytes} | tar -xf - -C "$bootstrap"
if ! LC_ALL=C git bundle unbundle - >/dev/null 2>"$bootstrap/bundle-error"; then
    cat "$bootstrap/bundle-error" >&2
    if grep -Fq {shlex.quote(MISSING_PREREQUISITE_MESSAGE)} "$bootstrap/bundle-error"; then
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
./scripts/lib/py "$bootstrap/runner.py" "$bootstrap/request.json"'''
    lock = guest_attempt_command('lock', armed, RUN_LOCK_FD)
    start = guest_attempt_command('start', armed, RUN_LOCK_FD)
    locked_run = f'''set -euo pipefail
{lock}
exec timeout --signal=KILL {settings.guest_timeout_seconds} {start} "$BASHPID" {shlex.quote(checkout)}'''
    return guest_checkout_shell(settings) + f'\nexec setsid --wait {open_run_lock_command(RUN_LOCK_FD, locked_run)}'


def cleanup_shell(settings: GuestSettings, armed: ArmedGuestAttempt) -> str:
    cancel = guest_attempt_command('cancel', armed, 8)
    # Cleanup can run before the guest has a checkout with scripts.
    group_helper = Path(process_group.__file__).read_text(encoding='utf-8')
    send_group = shlex.join(('python3', '-I', '-S', '-c', group_helper))
    cleanup = f'''set -euo pipefail
lock="/proc/$$/fd/8"
prove_end() {{
    flock -x -n 8 || return 1
    if ! {cancel}; then
        exit {GUEST_END_PENDING_STATUS}
    fi
    printf 'ended\\n'
    exit 0
}}
prove_end || :
read -r group owner < "$lock" || :
if [[ "$owner" == {shlex.quote(armed.attempt)} ]]; then
    if [[ "$group" =~ ^[1-9][0-9]{{0,9}}$ ]] && (( group > 1 && group <= 2147483647 )); then
        if [[ /proc/$group/fd/{RUN_LOCK_FD} -ef "$lock" ]]; then
            {send_group} "$group" KILL 2>/dev/null || :
        fi
    else
        printf 'The guest process group ID is invalid.\\n' >&2
    fi
fi
for ((retry = 0; retry < {GUEST_KILL_GRACE_SECONDS * 10}; retry++)); do
    prove_end || sleep 0.1
done
prove_end || :
printf 'The guest run lock is still held.\\n' >&2
exit {GUEST_END_PENDING_STATUS}'''
    return guest_checkout_shell(settings) + f'\nexec {open_run_lock_command(8, cleanup)}'


@dataclass(frozen=True)
class GuestEndProven:
    """The guest wrote the cancel record under the run lock."""


@dataclass(frozen=True)
class GuestEndPending:
    detail: str


def prove_guest_end(settings: GuestSettings, armed: ArmedGuestAttempt, *,
                    proof_deadline: float = math.inf) -> GuestEndProven | GuestEndPending:
    try:
        command = settings.command(cleanup_shell(settings, armed))
        remaining = proof_deadline - monotonic()
        if remaining <= 0:
            return GuestEndPending('The proof deadline expired before cleanup SSH.')
        reply = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace',
                               timeout=min(GUEST_CLEANUP_TIMEOUT_SECONDS, remaining), check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return GuestEndPending(str(error) or type(error).__name__)
    if reply.returncode == 0 and reply.stdout.strip() == 'ended':
        return GuestEndProven()
    reason = reply.stderr.strip() or f'cleanup SSH exited {reply.returncode}'
    return GuestEndPending(reason)


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


def stream_guest_bundle(repo: Path, command: list[str], bootstrap: bytes,
                        timeout: int) -> subprocess.CompletedProcess[bytes]:
    with subprocess.Popen(command, cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True) as stream:
        try:
            output, errors = stream.communicate(bootstrap, timeout=timeout)
        except BaseException as error:
            stop_ssh_process(stream)
            if isinstance(error, subprocess.TimeoutExpired):
                raise GuestUnavailable(f'SSH or guest run exceeded {timeout} seconds.') from error
            raise
    return subprocess.CompletedProcess(command, stream.returncode, output, errors)


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
    armed: ArmedGuestAttempt | None = None
    stream: subprocess.CompletedProcess[bytes] | None = None
    first_error: BaseException | None = None
    first_failure = ''
    proof_deadline: float | None = None
    retry_proof = False
    proof_obtained = False

    while True:
        try:
            # Protect the error handler and the jump to the next attempt.
            while True:
                # Keep the jump target inside the outer try.
                if proof_obtained:
                    break
                try:
                    if armed is None:
                        armed = arm_guest(settings, uuid4().hex)
                        command = settings.command(run_shell(settings, source, len(bootstrap), armed))
                        arguments = ['bash', '-o', 'pipefail', '-c', STREAM_SOURCE, TARGET, prefix, base, header, *command]
                        stream = stream_guest_bundle(repo, arguments, bootstrap, settings.transfer_timeout_seconds)
                    now = monotonic()
                    if proof_deadline is None:
                        proof_deadline = now + settings.unreachable_wait_seconds
                    remaining = proof_deadline - now
                    if remaining <= 0:
                        break
                    if retry_proof:
                        sleep(min(SSH_CONNECT_TIMEOUT_SECONDS, remaining))
                        if monotonic() >= proof_deadline:
                            break
                    retry_proof = True
                    proof = prove_guest_end(settings, armed, proof_deadline=proof_deadline)
                    if monotonic() >= proof_deadline:
                        break
                    if isinstance(proof, GuestEndProven):
                        proof_obtained = True
                    elif not first_failure:
                        first_failure = proof.detail
                except BaseException as error:
                    if armed is None:
                        raise
                    # Report the first interrupt even if the transfer failed before it.
                    if first_error is None or (isinstance(first_error, Exception) and not isinstance(error, Exception)):
                        first_error = error
            break
        except BaseException as error:
            if armed is None:
                # The route can send a run only after the guest confirms the arm.
                raise
            if first_error is None or (isinstance(first_error, Exception) and not isinstance(error, Exception)):
                first_error = error

    diagnostics = stream.stderr.decode(errors='replace') if stream is not None and stream.returncode else ''
    detail = ' '.join(diagnostics.split())[-1200:]
    if not proof_obtained:
        guest = settings.ssh_hops[-1]
        error = GuestUnavailable(f'guest unreachable: {guest} did not confirm the run ended '
                                 + f'within {settings.unreachable_wait_seconds} seconds ({target_config.UNREACHABLE_WAIT_KEY})')
        if detail:
            error.add_note(detail)
        raise error

    cleanup_detail = 'Guest cleanup: ended' + (f' after {first_failure}' if first_failure else '')
    if first_error is not None:
        first_error.add_note(cleanup_detail)
        raise first_error
    assert stream is not None
    if stream.returncode:
        if stream.returncode == MISSING_PREREQUISITE_STATUS and MISSING_PREREQUISITE_MESSAGE in diagnostics:
            raise GuestMissingPrerequisite(diagnostics.strip())
        raise GuestUnavailable(f'SSH or source stream exited {stream.returncode}: {detail or "no diagnostic"}. '
                               + cleanup_detail)
    result, _, count = stream.stdout.rstrip().rpartition(b'\n')
    return GuestReply(result.decode('utf-8'), int(count) - len(bootstrap), form, reason)


def transfer_source(repo: Path, settings: GuestSettings, source: CandidateSource, request: GuestRequest) -> GuestReply:
    try:
        guest = read_guest_main(settings)
    except (GuestUnavailable, OSError, subprocess.SubprocessError) as error:
        from scripts.mac_test import offload

        try:
            delay = conf.read('mac_test').number('unreachable_backoff_s')
            offload.write_backoff(time() + delay, TARGET, strict=True)
        except (conf.Unusable, OSError) as backoff_error:
            error.add_note(f'Cannot record the {TARGET} backoff: {backoff_error}')
        raise
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
    test_jobs = tuple(job for job in run.jobs if job.kind is GateJobKind.TEST_MODULE)
    modules = tuple(f'scripts/{job.suite}/{job.module}' for job in test_jobs)
    if len(modules) != len(source.modules) or frozenset(modules) != source.modules:
        raise GuestUnavailable('the guest result has a different module list')
    type_jobs = tuple((job.kind, job.suite, job.module) for job in run.jobs if job.kind is not GateJobKind.TEST_MODULE)
    expected = ((GateJobKind.TYPE_CHECK, *TYPE_CHECK_JOB),) if request['type_dirs'] else ()
    if type_jobs != expected:
        raise GuestUnavailable('the guest result has a different type-check job list')
    if row.get('verdict') != ('pass' if run.passed else 'fail'):
        raise GuestUnavailable('the guest result has an invalid verdict')
    skips = {path: job.times['skipped'] for path, job in zip(modules, test_jobs, strict=True)
             if isinstance(job.times, dict)}
    return MacLinuxGateRun(run.jobs, run.seconds, run.peak_memory_mib, run.tests_reused, run.selection_certified,
                           run.executions, run.time_limit_report, coverage=source.coverage,
                           bundle_bytes=reply.bundle_bytes, bundle_form=reply.bundle_form,
                           bundle_reason=reply.bundle_reason, module_skips=skips)


def route(repo: Path, *, mode: SuiteMode, full: bool, module_time_limit_s: float,
          selected: AllModules | TreeSelection, gate_job: StepJob | GateJobNotRecorded,
          type_dirs: Sequence[str] = ()) -> GateRun | str:
    """Return the guest gate result or the reason the guest route failed."""
    from scripts.aws_target import cloud_gate
    from scripts.build_queue.admission_client import step_identity
    from scripts.production import affected_modules, gate

    try:
        registry = conf.read('build_targets')
        if not eligibility.mac_linux_enabled(registry):
            return f'{TARGET} is off (build_targets.conf enabled.{TARGET}=off)'
        settings = read_settings(registry)
        if isinstance(eligibility.read_backoff(time(), TARGET), eligibility.ActiveBackoff):
            return f'{TARGET}: guest unreachable during retry backoff'
        if not math.isfinite(module_time_limit_s) or module_time_limit_s <= 0:
            raise GuestUnavailable('the module time limit must be positive and finite')
        repo = repo.resolve()
        unit = gate_job.unit if isinstance(gate_job, StepJob) else ''
        if not unit:
            unit = step_identity(mode.value, str(repo)).unit
        if not unit:
            raise GuestUnavailable('the gate has no owner unit for a numbered copy')
        source = candidate_source(repo, selected)
        relative_type_dirs = [(repo / name).resolve().relative_to(repo).as_posix() for name in type_dirs]
        request = GuestRequest(attempt=uuid4().hex, commit=source.commit, tree=source.tree,
                               origin_main=source.origin_main, mode=mode.wire, full=full,
                               selection=list(selected.paths) if isinstance(selected, TreeSelection) else 'all',
                               type_dirs=relative_type_dirs,
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
        notes = cast(list[str], getattr(error, '__notes__', []))
        return f'{TARGET}: ' + ' '.join((str(error), *notes))


def sandboxed_suite_command(tmp: Path, command: Sequence[str]) -> list[str]:
    # Mount the guest root before mounting proc for the new PID namespace.
    return ['bwrap', '--die-with-parent', '--unshare-pid', '--dev-bind', '/', '/',
            '--proc', '/proc', '--bind', str(tmp), '/tmp', '--tmpfs', '/dev/shm', '--', *command]


def run_guest(request_path: Path) -> int:
    """Take a numbered copy before starting the guest gate."""
    from scripts.lib import build_slots

    request = guest_request(request_path)
    main = Path.cwd()
    with (build_folders.inherit_build_lock(RUN_LOCK_FD),
          build_slots.take_copy(main, request['commit'], showrunner='', unit=request['unit'],
                                work=request['mode'], purpose=TARGET) as copy):
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
            if request['type_dirs']:
                environment['GATE_STATE_DIR'] = str(runtime / 'gate-state')
            # The -c entry selects the numbered copy as the Python checkout.
            command = ['./scripts/lib/py', '-X', f'cpu_count={request["jobs"]}',
                       '-c', 'import runpy, sys; runpy.run_path(sys.argv[1], run_name="__main__")',
                       str(Path(__file__).resolve()), str(request_path), '--run-copy']
            if shutil.which('dbus-run-session'):
                command[:0] = ['dbus-run-session', '--']
            return subprocess.run(sandboxed_suite_command(tmp, command), cwd=copy.path, env=environment,
                                  stdin=subprocess.DEVNULL, check=False,
                                  pass_fds=build_folders.held_build_folder_descriptors()).returncode


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
    type_dirs = tuple((repo / name).resolve() for name in request['type_dirs'])
    if any(not path.is_relative_to(repo) for path in type_dirs):
        raise GuestUnavailable('A type-check path leaves the numbered copy.')
    if type_dirs:
        for key, program in (('GATE_BASEDPYRIGHT', 'basedpyright'), ('GATE_UV', 'uv')):
            tool = os.environ.get(key, program)
            if shutil.which(tool) is None:
                raise GuestUnavailable(f'The guest cannot find the type-check tool {tool}.')
    descriptors = (int(os.environ[build_folders.NUMBERED_COPY_LOCK_ENV]), RUN_LOCK_FD)
    refs = subprocess.check_output(['git', 'rev-parse', 'HEAD', ORIGIN_MAIN, 'HEAD^{tree}'], cwd=repo,
                                   text=True, pass_fds=descriptors).splitlines()
    commit, origin_main, tree = refs
    if commit != request['commit'] or origin_main != request['origin_main']:
        raise GuestUnavailable('The guest checkout refs differ from the request.')
    if tree != request['tree']:
        raise GuestUnavailable('The guest checkout differs from the request.')
    selection = request['selection']
    machine = gate.WholeMachine(gate_parent=build_folders.ExplicitBuildParent(Path(os.environ['TMPDIR'])))
    token = copies.COMMAND_LOCKS.set(descriptors)
    try:
        with build_folders.inherit_build_lock(RUN_LOCK_FD), redirect_stdout(sys.stderr):
            run = gate.run_suites(repo, type_dirs=tuple(map(str, type_dirs)), full=request['full'],
                                  mode=SuiteMode(request['mode']), machine=machine,
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
        raise SystemExit(run_guest(Path(sys.argv[1])))

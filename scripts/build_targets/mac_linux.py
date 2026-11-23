"""Run the gate, or one argv of a hand made run, in the Mac's Linux guest.

Wait for proof that the guest run ended.
If the wait expires, fail the route. The guest keeps its run lock until the run ends.
If the gate parent and module supervisor die, a descendant can keep the lock and prevent proof.
Without a signal handler, Python can raise KeyboardInterrupt at any bytecode. An interrupt outside the protected block acts like process death.
When this process ends by any signal, the link to the guest ends, and the guest ends the run. The guest timeout still ends the run.
"""
from __future__ import annotations

from collections.abc import Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum
import errno
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
from time import monotonic, sleep, time
from typing import TYPE_CHECKING, Literal, TypedDict, cast, final
from urllib.parse import urlsplit
from uuid import uuid4

from scripts.aws_target import gate_box, ledger
from scripts.build_queue.admission_jobs import GateJobNotRecorded, StepJob
from scripts.build_targets import target_config
from scripts.lib import build_folders, conf, process_group
from scripts.mac_test import claim_state, eligibility
from scripts.production.gate_modules import GateRefused, discover_test_modules
from scripts.production.gate_routes import AllModules, GateJobKind, GateRun, ModuleCoverage, SuiteMode, TreeSelection
from scripts.test_times.gate_record import TYPE_CHECK_JOB

if TYPE_CHECKING:
    from scripts.build_queue.placement import PlacementAssignment


TARGET = eligibility.MAC_LINUX_TARGET
# The guest route holds a repository claim on this Mac target while its run lasts.
GUEST_CLAIM_TARGET = 'mac'
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
# The argv word after the request that makes the runner run the gate in the numbered copy. The reply descriptor
# number comes after it.
RUN_COPY = '--run-copy'
ARM_RECORD_PREFIX = 'mac-linux-armed'
# The guest prints this line, with the attempt, before it starts the argv of a command request.
RUN_START = 'mac-linux run start'
# The guest prints this line, with the attempt, to stderr when it has taken the run lock.
RUN_LOCKED = 'mac-linux run locked'
# The guest prints this line, with the attempt, when it refuses a command word; it starts nothing after it.
RUN_REFUSED = 'mac-linux run refused'
# The guest stream exits with this status when the guest took no run lock, so the guest started nothing.
GUEST_NOT_LOCKED_STATUS = 77
# Folders of installed tools, on natedev and on the guest. A program found in one of them is a tool, not a program
# file that the hand made check reads. They are here because the guest runner can read only this module.
TOOL_FOLDERS = (Path('/nix/store'), Path('/usr'), Path('/bin'), Path('/sbin'))
# The first bytes of a compiled program file.
COMPILED_START = b'\x7fELF'
# The read limit of a hand made run, on natedev and on the guest, is here for the same reason;
# scripts/production/hand_made_limit.py adds the natedev paths.
# The folders of the system that the run reads: the tool folders, the system profile and /etc. /bin and /usr hold
# the /bin/sh and /usr/bin/env links. A missing folder is not bound.
SYSTEM_FOLDERS = (*TOOL_FOLDERS, Path('/run/current-system'), Path('/etc'))
# The system folders in which a user can own an entry; the run does not see such an entry.
OWNED_FOLDERS = (Path('/etc'), Path('/bin'), Path('/usr'), Path('/sbin'))
# No one can change a file here, so a read only bind can have a link into it.
STORE = Path('/nix/store')
# The run sees its attempt temp folder at this path, and TMPDIR names it.
TEMP = Path('/tmp')
# The socket of the private sccache server of the run, in the attempt temp folder.
SCCACHE_SOCKET = TEMP / 'sccache.sock'
# The lock files of the cargo registry and git folders, at the top of CARGO_HOME.
CARGO_LOCKS = ('.package-cache', '.package-cache-mutate', '.global-cache')
# The key of the bwrap status line that gives the exit status of the program.
EXIT_STATUS = re.compile(rb'"exit-code"\s*:')
# The guest starts the argv with this shell in the limit: it prints the start line ($0), then runs the argv. So a
# limit that bwrap does not set up prints no start line.
START_SHELL = 'printf "%s\\n" "$0" && exec "$@"'
BWRAP_MISSING = 'bwrap is not on PATH, so the run cannot start under the read limit; the program did not start'
BWRAP_FAILED = ('bwrap did not start the program under the read limit (status {status}); the program did not start, '
                + 'and the output above gives the cause')
# The tree entry modes of a regular file.
FILE_MODES = ('100644', '100755')
CANCEL_RECORD_PREFIX = 'mac-linux-cancel'


class GuestUnavailable(RuntimeError):
    """The guest cannot return a completed gate."""


class GuestNotLocked(GuestUnavailable):
    """The guest took no run lock, so it started nothing and has no end to prove."""


class GuestRefusedCommand(GuestUnavailable):
    """The guest refused a command word before the start, so it started nothing."""


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


def mac_linux_reservation(repo: Path, work: SuiteMode | StepJob,
                          gate_job: StepJob | GateJobNotRecorded) -> AbstractContextManager[claim_state.ClaimIdentity]:
    """Take the Mac repository claim for the guest route."""
    from scripts.build_queue import mac_run

    return mac_run.held(repo, GUEST_CLAIM_TARGET, work, gate_job)


def consume_launch_permission(claim: claim_state.ClaimIdentity, assignment: PlacementAssignment) -> None:
    """Consume the launch permission of the gate assignment before the guest run starts.

    The gate process is the holder, so the queue keeps the guest assignment current until the gate ends.
    A refused consume, or a queue that does not answer, raises GuestTookNoRun: the guest starts nothing.
    """
    from scripts.build_queue import admission_client, admission_protocol as wire, placement
    from scripts.buildlog import store
    from scripts.lib import process_start

    start = process_start.process_start(os.getpid())
    if not isinstance(start, process_start.Started):
        raise GuestTookNoRun('the gate process has no start time to hold the launch permission')
    destination = assignment.destination
    reference = placement.ProviderClaimReference(destination.provider, destination.target, destination.scope,
                                                 claim.claim_id, assignment.episode, assignment.attempt)
    holder = placement.ClaimHolderIdentity(store.host_name(), os.getpid(), start.start)
    try:
        permission = admission_client.consume_permission(
            wire.ConsumePermission(admission_client.request_id(), assignment, reference, holder))
    except wire.AdmissionUnavailable as error:
        raise GuestTookNoRun(f'the launch permission is unavailable: {error}') from error
    if isinstance(permission, wire.DispatchStatus):
        raise GuestTookNoRun(f'the queue refused the launch permission: {permission.reason}')


class GuestRunRequest(TypedDict):
    """The fields of each guest request: the attempt, the commit to copy and the unit that owns the copy."""
    attempt: str
    commit: str
    tree: str
    origin_main: str
    unit: str


@final
class GuestRequest(GuestRunRequest):
    """Run the gate suites in the numbered copy; the result is one JSON line."""
    mode: str
    full: bool
    selection: list[str] | Literal['all']
    type_dirs: list[str]
    module_time_limit_s: float
    run_time_limit_seconds: int
    jobs: int


@final
class GuestCommandRequest(GuestRunRequest):
    """Run one argv in the numbered copy. Its output and exit status are those of the guest run.

    checked holds the path in the checkout of each file that natedev read for a word of the argv.
    """
    work: str
    argv: list[str]
    checked: list[str]


def route_errors() -> tuple[type[Exception], ...]:
    """The errors that end a guest route with a reason."""
    from scripts.production import affected_modules

    return (GuestUnavailable, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError,
            GateRefused, affected_modules.GraphError)


def new_run_settings() -> GuestSettings | GuestNotStarted | str:
    """The guest settings for a new run. A guest that is off gives the reason, and a guest in retry backoff
    gives GuestNotStarted."""
    registry = conf.read('build_targets')
    if not eligibility.mac_linux_enabled(registry):
        return f'{TARGET} is off (build_targets.conf enabled.{TARGET}=off)'
    settings = read_settings(registry)
    if isinstance(eligibility.read_backoff(time(), TARGET), eligibility.ActiveBackoff):
        return GuestNotStarted('guest unreachable during retry backoff')
    return settings


@dataclass(frozen=True)
class CheckoutSource:
    """One commit of the checkout with its dirty files, and the origin/main that the guest main gets."""
    head: str
    commit: str
    tree: str
    origin_main: str
    origin_url: str


@dataclass(frozen=True)
class CandidateSource(CheckoutSource):
    modules: frozenset[str]
    coverage: ModuleCoverage


def check_repository(repo: Path) -> None:
    """Refuse a checkout of a repository other than claude; the guest holds only the claude checkout."""
    repository = build_folders.repo_word(repo)
    if repository != 'claude':
        raise GuestRepositoryRefused(repository)


def checkout_source(repo: Path) -> CheckoutSource:
    from scripts.aws_target import cloud_gate
    from scripts.production.gate import checked_git

    check_repository(repo)
    head = checked_git(repo, 'rev-parse', 'HEAD')
    origin_main = checked_git(repo, 'rev-parse', '--verify', f'{ORIGIN_MAIN}^{{commit}}')
    tree, dirty = cloud_gate.working_tree(repo)
    origin_url = checked_git(repo, 'remote', 'get-url', 'origin')
    origin = urlsplit(origin_url)
    if origin.password or origin.query or ('://' in origin_url and origin.username):
        raise GuestUnavailable('the origin URL must contain no credential')
    commit = checked_git(repo, 'commit-tree', tree, '-p', head, '-m', 'Mac Linux gate snapshot') if dirty else head
    return CheckoutSource(head, commit, tree, origin_main, origin_url)


def blob_id(data: bytes, object_id_length: int) -> str:
    """The Git blob id of the bytes, for the object format of an id of that length."""
    digest = hashlib.sha256() if object_id_length == 64 else hashlib.sha1(usedforsecurity=False)
    digest.update(b'blob %d\0' % len(data) + data)
    return digest.hexdigest()


def tree_mismatches(repo: Path, tree: str, files: Mapping[str, bytes]) -> list[str]:
    """Each checked file, by its path in the checkout, that the tree lacks as a regular file with the checked bytes."""
    from scripts.production.gate import checked_git

    if not files:
        return []
    entries: dict[str, tuple[str, str]] = {}
    for entry in checked_git(repo, '--literal-pathspecs', 'ls-tree', '-z', tree, '--', *files).split('\0'):
        if entry:
            fields, name = entry.split('\t', 1)
            mode, _, blob = fields.split()
            entries[name] = (mode, blob)
    return [name for name, data in files.items()
            if (entry := entries.get(name)) is None or entry[0] not in FILE_MODES
            or entry[1] != blob_id(data, len(tree))]


def run_start_line(attempt: str) -> str:
    return f'{RUN_START} {attempt}'


def run_locked_line(attempt: str) -> str:
    return f'{RUN_LOCKED} {attempt}'


def run_refused_line(attempt: str) -> str:
    return f'{RUN_REFUSED} {attempt}'


def candidate_source(repo: Path, selected: AllModules | TreeSelection) -> CandidateSource:
    source = checkout_source(repo)
    paths = frozenset(str(path.relative_to(repo)) for _, path in discover_test_modules(repo))
    modules = paths
    if isinstance(selected, TreeSelection):
        if selected.tree != source.tree:
            raise GuestUnavailable('the checkout changed after module selection')
        modules = frozenset(selected.paths)
        if not modules <= paths:
            raise GuestUnavailable('selected modules are absent from the checkout')
    coverage = ModuleCoverage.COMPLETE if modules == paths else ModuleCoverage.SUBSET
    return CandidateSource(source.head, source.commit, source.tree, source.origin_main, source.origin_url,
                           modules, coverage)


def runner_bootstrap(request: GuestRequest | GuestCommandRequest) -> bytes:
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


GUEST_CHECKOUT_SETUP = '''
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


class ArmedGuestAttemptRecord(TypedDict):
    """Save the proof inputs for a kept guest claim."""

    attempt: str
    machine_id: str
    boot_id: str
    deadline_ns: int
    guest_user: str
    checkout: str


@dataclass(frozen=True)
class ArmedGuestAttempt:
    """Identify the guest, its account and checkout, the run, and its deadline."""

    attempt: str
    machine_id: str
    boot_id: str
    deadline_ns: int
    guest_user: str
    checkout: str

    def to_record(self) -> ArmedGuestAttemptRecord:
        return ArmedGuestAttemptRecord(attempt=self.attempt, machine_id=self.machine_id,
                                       boot_id=self.boot_id, deadline_ns=self.deadline_ns,
                                       guest_user=self.guest_user, checkout=self.checkout)

    @classmethod
    def from_record(cls, record: ArmedGuestAttemptRecord) -> ArmedGuestAttempt:
        return cls(**record)


class GuestTookNoRun(GuestUnavailable):
    """The guest did not arm a run, or the run stopped before a test started, so natedev can take the gate."""


@dataclass(frozen=True)
class GuestNotStarted:
    """The guest took no run: the Mac refused the claim, the guest did not arm, or no test started.

    The reason names only the cause; the gate says that the guest took no run.
    """

    reason: str


class GuestEndUnproven(GuestUnavailable):
    """The proof wait expired for an armed guest attempt."""

    def __init__(self, message: str, armed: ArmedGuestAttempt) -> None:
        super().__init__(message)
        self.armed: ArmedGuestAttempt = armed


ARM_ATTEMPT = '''\
import os
import sys
import time

prefix, attempt, window = sys.argv[1:]
if not attempt or any(character not in '0123456789abcdefghijklmnopqrstuvwxyz-' for character in attempt):
    sys.exit('The guest attempt ID is invalid.')
with open('/etc/machine-id', encoding='ascii') as identity:
    machine = identity.read().rstrip('\\n')
if len(machine) != 32 or any(character not in '0123456789abcdef' for character in machine):
    sys.exit('The guest machine ID is invalid.')
with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as identity:
    boot = identity.read().strip()
git = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
deadline = time.clock_gettime_ns(time.CLOCK_BOOTTIME) + int(window) * 10**9
record = f'{prefix}.{boot}.{deadline}.{attempt}'
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
os.close(os.open(record, flags, 0o600, dir_fd=git))
os.close(git)
print(f'armed {machine} {boot} {deadline} {os.getcwd()}', flush=True)
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
    command = settings.command(guest_shell(settings) + GUEST_CHECKOUT_SETUP + f'\nexec {arm}')
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
    reply = re.fullmatch(rb'armed ([0-9a-f]{32}) ([0-9a-f-]{36}) ([0-9]{1,20}) (/[^\n\0]*)\n', output)
    if reply is None:
        raise GuestUnavailable('the guest did not acknowledge the armed deadline')
    return ArmedGuestAttempt(attempt, reply[1].decode('ascii'), reply[2].decode('ascii'), int(reply[3]),
                             settings.guest_user, reply[4].decode(errors='replace'))


def armed_guest_checkout_shell(settings: GuestSettings, armed: ArmedGuestAttempt) -> str:
    return guest_shell(settings) + f'''
machine_id=$(cat /etc/machine-id) || exit {GUEST_END_PENDING_STATUS}
if [ "$machine_id" != {shlex.quote(armed.machine_id)} ]; then
    printf 'A different guest answered the SSH request. The machine ID differs from the armed guest.\\n' >&2
    exit {GUEST_END_PENDING_STATUS}
fi
armed_checkout=$(cd -P -- "$checkout" 2>/dev/null && pwd -P) || armed_checkout=''
if [ "$armed_checkout" != {shlex.quote(armed.checkout)} ]; then
    printf 'The guest checkout differs from the armed checkout %s.\\n' {shlex.quote(armed.checkout)} >&2
    exit {GUEST_END_PENDING_STATUS}
fi''' + GUEST_CHECKOUT_SETUP


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


def guest_group_signal_command() -> str:
    """The guest command that sends SIGNAL to process group GROUP through the shared check in process_group.py.

    Append the GROUP and SIGNAL arguments. The guest can run it before its checkout has scripts, so the command
    carries the source of the module.
    """
    return shlex.join(('python3', '-I', '-S', '-c', Path(process_group.__file__).read_text(encoding='utf-8')))


def run_shell(settings: GuestSettings, source: CheckoutSource, bootstrap_bytes: int, armed: ArmedGuestAttempt) -> str:
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
# natedev holds the link open for the run. If the link ends first, end the run group, which frees the run lock.
# The watcher is in the run group, so a group kill ends it too; the EXIT trap ends it after a normal end.
# At the link end it moves to a new session, because the shared check refuses the caller's own group.
read -r group _ < "/proc/$$/fd/{RUN_LOCK_FD}"
{{ while read -r _; do :; done; exec setsid {guest_group_signal_command()} "$group" KILL; }} <&0 >/dev/null 2>&1 {RUN_LOCK_FD}>&- &
watcher=$!
trap 'kill "$watcher" 2>/dev/null || :; rm -rf -- "$bootstrap"' EXIT
exec </dev/null
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
{lock} || exit {GUEST_NOT_LOCKED_STATUS}
printf '%s\\n' {shlex.quote(run_locked_line(armed.attempt))} >&2
exec timeout --signal=KILL {settings.guest_timeout_seconds} {start} "$BASHPID" {shlex.quote(checkout)}'''
    return armed_guest_checkout_shell(settings, armed) + f'\nexec setsid --wait {open_run_lock_command(RUN_LOCK_FD, locked_run)}'


def cleanup_shell(settings: GuestSettings, armed: ArmedGuestAttempt) -> str:
    cancel = guest_attempt_command('cancel', armed, 8)
    send_group = guest_group_signal_command()
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
    return armed_guest_checkout_shell(settings, armed) + f'\nexec {open_run_lock_command(8, cleanup)}'


@dataclass(frozen=True)
class GuestEndProven:
    """The guest wrote the cancel record under the run lock."""


@dataclass(frozen=True)
class GuestEndPending:
    detail: str


def prove_guest_end(settings: GuestSettings, armed: ArmedGuestAttempt, *,
                    proof_deadline: float = math.inf) -> GuestEndProven | GuestEndPending:
    if settings.guest_user != armed.guest_user:
        # The run lock lives in the armed account's checkout; another account cannot prove its end.
        return GuestEndPending(f'The guest account changed from {armed.guest_user} to {settings.guest_user}.')
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


def prove_kept_guest_end(armed: ArmedGuestAttempt) -> GuestEndProven | GuestEndPending:
    """Check whether the guest run for a kept claim has ended."""
    try:
        settings = read_settings(conf.read('build_targets'))
    except (GuestUnavailable, OSError, ValueError) as error:
        return GuestEndPending(str(error) or type(error).__name__)
    return prove_guest_end(settings, armed)


@dataclass
class GuestEndWait:
    """Ask the guest for proof that the run ended, until it gives it or the unreachable wait expires.

    The wait starts at the first request. A call after an interrupt continues with the same deadline.
    """

    settings: GuestSettings
    armed: ArmedGuestAttempt
    deadline: float | None = None
    retry: bool = False
    proven: bool = False
    first_failure: str = ''

    def run(self) -> None:
        while not self.proven:
            now = monotonic()
            if self.deadline is None:
                self.deadline = now + self.settings.unreachable_wait_seconds
            remaining = self.deadline - now
            if remaining <= 0:
                return
            if self.retry:
                sleep(min(SSH_CONNECT_TIMEOUT_SECONDS, remaining))
                if monotonic() >= self.deadline:
                    return
            self.retry = True
            proof = prove_guest_end(self.settings, self.armed, proof_deadline=self.deadline)
            if monotonic() >= self.deadline:
                return
            if isinstance(proof, GuestEndProven):
                self.proven = True
            elif not self.first_failure:
                self.first_failure = proof.detail

    def unreachable(self) -> GuestEndUnproven:
        guest = self.settings.ssh_hops[-1]
        return GuestEndUnproven(f'guest unreachable: {guest} did not confirm the run ended '
                                + f'within {self.settings.unreachable_wait_seconds} seconds ({target_config.UNREACHABLE_WAIT_KEY})',
                                self.armed)

    def cleanup_detail(self) -> str:
        return 'Guest cleanup: ended' + (f' after {self.first_failure}' if self.first_failure else '')


def first_raised(first: BaseException | None, error: BaseException) -> BaseException:
    """Keep the first error. An interrupt replaces an earlier error that is not an interrupt."""
    return error if first is None or (isinstance(first, Exception) and not isinstance(error, Exception)) else first


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
class BundleSend:
    """The arguments of the bundle stream, and the form and reason that the gate result records."""

    base: str
    header: str
    form: BundleForm
    reason: BundleReason


@dataclass(frozen=True)
class GuestReply:
    result: str
    bundle_bytes: int
    bundle_form: BundleForm
    bundle_reason: BundleReason
    stream_report: str
    tests_started: bool


@contextmanager
def bundle_refs(repo: Path, source: CheckoutSource, attempt: str) -> Generator[str]:
    """Keep named bundle tips for the duration of the transfer."""
    from scripts.production.gate import checked_git

    prefix = f'refs/mac-linux/{attempt}'
    active_error: BaseException | None = None
    try:
        _ = checked_git(repo, 'update-ref', f'{prefix}/candidate', source.commit)
        _ = checked_git(repo, 'update-ref', f'{prefix}/main', source.origin_main)
        yield prefix
    except BaseException as error:
        active_error = error
        raise
    finally:
        failure = active_error
        for tip in ('candidate', 'main'):
            ref = f'{prefix}/{tip}'
            try:
                _ = checked_git(repo, 'update-ref', '-d', ref)
            except BaseException as error:
                if failure is None:
                    failure = error
                elif (isinstance(error, Exception) or isinstance(failure, GuestEndUnproven)
                      or not isinstance(failure, Exception)):
                    failure.add_note(f'Cannot remove bundle ref {ref}: {error}')
                else:
                    # An interrupt must end the route, never a retry that catches the earlier error.
                    error.add_note(f'Interrupted while handling: {failure!r}')
                    failure = error
        if failure is not None and failure is not active_error:
            raise failure


def empty_delta_header(repo: Path, source: CheckoutSource, basis: DeltaBundle, prefix: str) -> str:
    from scripts.production.gate import checked_git

    if checked_git(repo, 'rev-list', '--max-count=1', source.commit, source.origin_main, f'^{basis.base}'):
        return ''
    object_format = 'sha256' if len(source.commit) == 64 else 'sha1'
    # Git refuses to create a bundle when both tips are already in the prerequisite.
    return (f'# v3 git bundle\n@object-format={object_format}\n-{basis.base} guest main\n'
            + f'{source.commit} {prefix}/candidate\n{source.origin_main} {prefix}/main\n\n')


# Without a count argument, the SSH output is the stream output. With one, send the SSH result to
# descriptor 3, use descriptor 4 to collect the byte count, and print the count last.
# After the bundle, the SSH input stays open while natedev holds the link (guest_link). The guest ends the run
# when its input ends first.
STREAM_SOURCE = '''
prefix=$1
base=$2
header=$3
count=$4
exec {link}<&"$5"
shift 5
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
send() {
    if [ -n "$count" ]; then
        { cat && bundle; } | tee >(wc -c >&4) || exit
    else
        { cat && bundle; } || exit
    fi
    exec cat <&"$link"
}
linked_ssh() {
    exec {input}< <(send)
    sender=$!
    "$@" <&"$input" {input}<&- 3>&- 4>&-
    status=$?
    exec {input}<&-
    kill "$sender" 2>/dev/null
    return "$status"
}
if [ -z "$count" ]; then
    linked_ssh "$@"
    exit
fi
exec 3>&1
size=$(
    exec 4>&1
    linked_ssh "$@" >&3
) || exit "$?"
printf '\\n%s\\n' "$size"
'''


@contextmanager
def guest_link() -> Generator[int]:
    """Hold the link to the guest run for the block, and yield the descriptor that the stream reads.

    The link ends when the block ends, or when this process ends by any signal. The guest then ends a run that
    is still active, and so frees the run lock with no cleanup from natedev.
    """
    link, hold = os.pipe()
    try:
        yield link
    finally:
        os.close(hold)
        os.close(link)


def stream_guest_bundle(repo: Path, command: list[str], bootstrap: bytes, timeout: int,
                        link: int) -> subprocess.CompletedProcess[bytes]:
    with subprocess.Popen(command, cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True, pass_fds=(link,)) as stream:
        try:
            output, errors = stream.communicate(bootstrap, timeout=timeout)
        except BaseException as error:
            stop_ssh_process(stream)
            if isinstance(error, subprocess.TimeoutExpired):
                raise GuestUnavailable(f'SSH or guest run exceeded {timeout} seconds.') from error
            raise
    return subprocess.CompletedProcess(command, stream.returncode, output, errors)


def bundle_send(repo: Path, source: CheckoutSource, bundle: FullBundle | DeltaBundle, prefix: str) -> BundleSend:
    if isinstance(bundle, DeltaBundle):
        return BundleSend(bundle.base, empty_delta_header(repo, source, bundle, prefix), BundleForm.DELTA, BundleReason.DELTA)
    return BundleSend('', '', BundleForm.FIRST, bundle.reason)


def stream_arguments(settings: GuestSettings, source: CheckoutSource, bootstrap_bytes: int, armed: ArmedGuestAttempt,
                     prefix: str, send: BundleSend, *, count: bool, link: int) -> list[str]:
    """The local command that sends the bootstrap and the bundle to the armed guest run, then holds the link."""
    command = settings.command(run_shell(settings, source, bootstrap_bytes, armed))
    return ['bash', '-o', 'pipefail', '-c', STREAM_SOURCE, TARGET, prefix, send.base, send.header,
            'count' if count else '', str(link), *command]


def run_remote(repo: Path, settings: GuestSettings, source: CandidateSource, request: GuestRequest,
               prefix: str, bundle: FullBundle | DeltaBundle) -> GuestReply:
    bootstrap = runner_bootstrap(request)
    send = bundle_send(repo, source, bundle, prefix)
    armed: ArmedGuestAttempt | None = None
    end: GuestEndWait | None = None
    stream: subprocess.CompletedProcess[bytes] | None = None
    first_error: BaseException | None = None

    while True:
        try:
            # Protect the error handler and the jump to the next attempt.
            while True:
                # Keep the jump target inside the outer try.
                if end is not None and end.proven:
                    break
                try:
                    if armed is None:
                        try:
                            armed = arm_guest(settings, request['attempt'])
                        except (GuestUnavailable, OSError, subprocess.SubprocessError) as error:
                            raise GuestTookNoRun(f'the guest did not arm the run: {error}') from error
                        with guest_link() as link:
                            arguments = stream_arguments(settings, source, len(bootstrap), armed, prefix, send,
                                                         count=True, link=link)
                            stream = stream_guest_bundle(repo, arguments, bootstrap,
                                                         settings.transfer_timeout_seconds, link)
                    if end is None:
                        end = GuestEndWait(settings, armed)
                    end.run()
                    break
                except BaseException as error:
                    if armed is None:
                        raise
                    # Report the first interrupt even if the transfer failed before it.
                    first_error = first_raised(first_error, error)
            break
        except BaseException as error:
            if armed is None:
                # The route can send a run only after the guest confirms the arm.
                raise
            first_error = first_raised(first_error, error)

    assert end is not None
    if not end.proven:
        error = end.unreachable()
        if stream is not None and stream.returncode:
            error.add_note(stream_report(stream))
        raise error

    cleanup_detail = end.cleanup_detail()
    if first_error is not None:
        first_error.add_note(cleanup_detail)
        raise first_error
    assert stream is not None
    return stream_reply(stream, request['attempt'], len(bootstrap), send, cleanup_detail)


def stream_reply(stream: subprocess.CompletedProcess[bytes], attempt: str, bootstrap_bytes: int, send: BundleSend,
                 cleanup_detail: str) -> GuestReply:
    """The reply line and the bundle size from the output of a guest stream that ended, or the guest failure."""
    diagnostics = stream.stderr.decode(errors='replace')
    tests_started = run_start_line(attempt) in diagnostics
    if stream.returncode:
        if stream.returncode == MISSING_PREREQUISITE_STATUS and MISSING_PREREQUISITE_MESSAGE in diagnostics:
            raise GuestMissingPrerequisite(diagnostics.strip())
        raise guest_failure(tests_started, f'{stream_report(stream)}. {cleanup_detail}')
    result, _, count = stream.stdout.decode(errors='replace').rstrip().rpartition('\n')
    try:
        bundle_bytes = int(count) - bootstrap_bytes
    except ValueError:
        raise guest_failure(tests_started, f'the guest stream gave no byte count ({count[-200:]!r}); '
                            + f'{stream_report(stream)}. {cleanup_detail}') from None
    return GuestReply(result, bundle_bytes, send.form, send.reason, stream_report(stream), tests_started)


def stream_report(stream: subprocess.CompletedProcess[bytes]) -> str:
    """The exit status and the last 1200 characters of the standard error of the guest stream."""
    detail = ' '.join(stream.stderr.decode(errors='replace').split())[-1200:]
    return f'SSH or source stream exited {stream.returncode}: {detail or "no diagnostic"}'


def guest_failure(tests_started: bool, reason: str) -> GuestUnavailable:
    """A failure before the first test lets natedev take the gate; a failure after it refuses the gate."""
    return (GuestUnavailable if tests_started else GuestTookNoRun)(reason)


def guest_bundle(repo: Path, settings: GuestSettings) -> FullBundle | DeltaBundle:
    """Probe the guest main commit, and leave out the history that the guest has.

    A failed probe records the retry backoff of the guest.
    """
    try:
        guest = read_guest_main(settings)
    except (GuestUnavailable, OSError, subprocess.SubprocessError) as error:
        from scripts.mac_test import offload

        took_no_run = GuestTookNoRun(f'the guest main probe failed: {error}')
        try:
            delay = conf.read('mac_test').number('unreachable_backoff_s')
            offload.write_backoff(time() + delay, TARGET, strict=True)
        except (conf.Unusable, OSError) as backoff_error:
            took_no_run.add_note(f'Cannot record the {TARGET} backoff: {backoff_error}')
        raise took_no_run from error
    if isinstance(guest, EmptyGuestMain):
        return FullBundle(BundleReason.FIRST_RUN)
    if subprocess.run(['git', 'cat-file', '-e', f'{guest.commit}^{{commit}}'], cwd=repo,
                      capture_output=True, timeout=TRANSFER_ALLOWANCE_SECONDS, check=False).returncode:
        return FullBundle(BundleReason.SOURCE_LACKS_BASE)
    return DeltaBundle(guest.commit)


def transfer_source(repo: Path, settings: GuestSettings, source: CandidateSource, request: GuestRequest) -> GuestReply:
    bundle = guest_bundle(repo, settings)
    with bundle_refs(repo, source, request['attempt']) as prefix:
        return run_remote(repo, settings, source, request, prefix, bundle)


@dataclass
class GuestCommand:
    """Run one argv in the guest checkout of a commit of the checkout, and prove that the guest run ended.

    start sends the commit and returns when the guest starts the argv. The output of the argv is the
    output of the stream process. A delta bundle that the guest cannot use is a start failure; the stream
    sends no full bundle after it.
    """

    settings: GuestSettings
    refs: ExitStack = field(default_factory=ExitStack)
    armed: ArmedGuestAttempt | None = None
    process: subprocess.Popen[bytes] | None = None

    def start(self, repo: Path, argv: Sequence[str], *, work: str, unit: str, bootstrap: Path,
              checked: Mapping[Path, bytes]) -> subprocess.Popen[bytes]:
        """Start one run; the bootstrap file holds the request and the runner while the stream reads them.

        checked holds the bytes of each file of the checkout that the run must find in the commit. A
        failure before the guest starts the argv raises GuestUnavailable with the guest output, and closes
        the output pipe. While process is None, the guest got no command and started nothing; after it is set,
        only GuestNotLocked and GuestRefusedCommand prove that the guest started nothing (wait_for_start). After
        GuestNotLocked, finish proves no end. The process has one output pipe for its standard output and
        standard error.
        """
        source = checkout_source(repo)
        files = {path.relative_to(repo).as_posix(): data for path, data in checked.items()}
        mismatches = tree_mismatches(repo, source.tree, files)
        if mismatches:
            raise GuestUnavailable(f'the commit for the guest does not hold the checked text of {", ".join(mismatches)}: '
                                   + 'the file changed after the check, or git does not keep it')
        request = GuestCommandRequest(attempt=uuid4().hex, commit=source.commit, tree=source.tree,
                                      origin_main=source.origin_main, unit=unit, work=work, argv=list(argv),
                                      checked=sorted(files))
        data = runner_bootstrap(request)
        _ = bootstrap.write_bytes(data)
        bundle = guest_bundle(repo, self.settings)
        prefix = self.refs.enter_context(bundle_refs(repo, source, request['attempt']))
        send = bundle_send(repo, source, bundle, prefix)
        self.armed = arm_guest(self.settings, request['attempt'])
        link = self.refs.enter_context(guest_link())
        arguments = stream_arguments(self.settings, source, len(data), self.armed, prefix, send, count=False,
                                     link=link)
        with bootstrap.open('rb') as stdin:
            self.process = subprocess.Popen(arguments, cwd=repo, stdin=stdin, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(link,))
        try:
            wait_for_start(self.process, request['attempt'])
        except BaseException as error:
            assert self.process.stdout is not None
            self.process.stdout.close()
            if isinstance(error, GuestNotLocked):
                self.armed = None
            raise
        return self.process

    def finish(self, failure: BaseException | None = None) -> None:
        """Prove the end of an armed run, then end the guest link and remove the bundle refs.

        failure is the error that ended the run early, if any. A stream that still runs is stopped, and the
        guest kills a run that still holds the lock. An interrupt does not stop the proof; the first one is
        raised after it. A guest that gives no proof raises GuestUnavailable, which names the first error,
        but an interrupt stays the raised error.
        """
        try:
            if self.armed is not None:
                self.prove_end(GuestEndWait(self.settings, self.armed), failure)
        finally:
            self.refs.close()

    def prove_end(self, end: GuestEndWait, failure: BaseException | None) -> None:
        first_error: BaseException | None = None
        while True:
            try:
                # Protect the error handler and the jump to the next attempt.
                while True:
                    # Keep the jump target inside the outer try.
                    if end.proven:
                        break
                    try:
                        if self.process is not None and self.process.poll() is None:
                            stop_ssh_process(self.process)
                        end.run()
                        break
                    except BaseException as error:
                        first_error = first_raised(first_error, error)
                break
            except BaseException as error:
                first_error = first_raised(first_error, error)
        if not end.proven:
            unreachable = end.unreachable()
            earlier = failure if first_error is None else first_raised(failure, first_error)
            if earlier is None:
                raise unreachable
            if not isinstance(earlier, Exception):
                earlier.add_note(str(unreachable))
                raise earlier
            raise GuestUnavailable(f'{unreachable}; the first error: {earlier}') from earlier
        if first_error is not None:
            first_error.add_note(end.cleanup_detail())
            raise first_error


def wait_for_start(process: subprocess.Popen[bytes], attempt: str) -> None:
    """Read the guest output up to the start line of the argv, one byte at a time, so that no argv output is read.

    Output before the start line, other than the lock and refused lines, goes to stderr. A stream that ends
    before the start line raises GuestUnavailable with that output. When it ends before the lock line with
    GUEST_NOT_LOCKED_STATUS, the error is GuestNotLocked. After the refused line, the error is GuestRefusedCommand.
    Both mean that the guest started nothing. A stream that ends with another status can come from a lost SSH
    link after the guest took the lock or printed the start line, so the start is unknown and needs the end proof.
    """
    assert process.stdout is not None
    descriptor = process.stdout.fileno()
    start = run_start_line(attempt).encode('utf-8') + b'\n'
    locked_line = run_locked_line(attempt).encode('utf-8') + b'\n'
    refused_line = run_refused_line(attempt).encode('utf-8') + b'\n'
    locked = refused = False
    before = bytearray()
    line = bytearray()
    while True:
        byte = os.read(descriptor, 1)
        if not byte:
            status = process.wait()
            text = (before + line).decode('utf-8', errors='replace').strip()
            error = (GuestNotLocked if status == GUEST_NOT_LOCKED_STATUS and not locked
                     else GuestRefusedCommand if refused else GuestUnavailable)
            raise error(text or f'the guest stream exited {status} before the argv started')
        line += byte
        if byte == b'\n':
            if bytes(line) == start:
                break
            if bytes(line) == locked_line:
                locked = True
            elif bytes(line) == refused_line:
                refused = True
            else:
                before += line
            line.clear()
    if before:
        _ = sys.stderr.buffer.write(bytes(before))
        _ = sys.stderr.flush()


@dataclass(frozen=True, kw_only=True)
class MacLinuxGateRun(GateRun):
    """Keep the bundle details and the skip counts from the guest."""

    bundle_bytes: int
    bundle_form: BundleForm
    bundle_reason: BundleReason
    module_skips: dict[str, int]


def completed_run(reply: GuestReply, request: GuestRequest, source: CandidateSource) -> MacLinuxGateRun:
    try:
        return reply_run(reply, request, source)
    except route_errors() as error:
        raise guest_failure(reply.tests_started, f'the guest reply is unusable ({error}); '
                            + f'reply start {reply.result[:200]!r}; {reply.stream_report}') from error


def reply_run(reply: GuestReply, request: GuestRequest, source: CandidateSource) -> MacLinuxGateRun:
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
          assignment: PlacementAssignment, type_dirs: Sequence[str] = ()) -> GateRun | GuestNotStarted | str:
    """Return the guest gate result, a run the guest did not take, or the reason the guest route failed.

    The assignment is the one the gate checked at its start. The route consumes its launch permission.
    """
    from scripts.aws_target import cloud_gate
    from scripts.build_queue import mac_run
    from scripts.build_queue.admission_client import step_identity
    from scripts.production import gate

    try:
        settings = new_run_settings()
        if not isinstance(settings, GuestSettings):
            return settings
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
        try:
            with mac_linux_reservation(repo, mode, gate_job) as claim:
                consume_launch_permission(claim, assignment)
                reply = transfer_source(repo, settings, source, request)
                run = completed_run(reply, request, source)
        except GuestMissingPrerequisite:
            request = request.copy()
            request['attempt'] = uuid4().hex
            with (mac_linux_reservation(repo, mode, gate_job),
                  bundle_refs(repo, source, request['attempt']) as prefix):
                reply = run_remote(repo, settings, source, request, prefix,
                                   FullBundle(BundleReason.GUEST_LACKS_PREREQUISITE))
                run = completed_run(reply, request, source)
        if gate.checked_git(repo, 'rev-parse', 'HEAD') != source.head or cloud_gate.working_tree(repo)[0] != source.tree:
            raise GuestUnavailable('the checkout changed during the guest run')
        return run
    except mac_run.MacRunRefused as error:
        return GuestNotStarted(str(error))
    except GuestTookNoRun as error:
        notes = cast(list[str], getattr(error, '__notes__', []))
        return GuestNotStarted(' '.join((str(error), *notes)))
    except route_errors() as error:
        notes = cast(list[str], getattr(error, '__notes__', []))
        return f'{TARGET}: ' + ' '.join((str(error), *notes))


def sandboxed_suite_command(tmp: Path, command: Sequence[str]) -> list[str]:
    # Mount the guest root before mounting proc for the new PID namespace.
    return ['bwrap', '--die-with-parent', '--unshare-pid', '--dev-bind', '/', '/',
            '--proc', '/proc', '--bind', str(tmp), '/tmp', '--tmpfs', '/dev/shm', '--', *command]


def guest_tool(real: Path, copy: Path) -> bool:
    """A real file path outside the copy that is in a tool folder or is a compiled file; natedev trusts the same."""
    if real.is_relative_to(copy):
        return False
    if any(real.is_relative_to(folder) for folder in TOOL_FOLDERS):
        return True
    try:
        with real.open('rb') as handle:
            return handle.read(len(COMPILED_START)) == COMPILED_START
    except OSError:
        return False


EntryKind = Literal['folder', 'file', 'link', 'other', 'missing']


def entry_kind(path: str, *, follow: bool = True) -> EntryKind:
    """The kind of the entry at the path: os.stat follows a link, and os.lstat does not. A file is a regular
    file; other is a FIFO, a socket or a device.

    ENOENT and ENOTDIR mean missing. Each other OSError, such as ENAMETOOLONG or ELOOP, is raised, so that
    the caller refuses the word and does not take the entry as missing.
    """
    try:
        mode = (os.stat if follow else os.lstat)(path).st_mode
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            return 'missing'
        raise
    if stat.S_ISDIR(mode):
        return 'folder'
    if stat.S_ISREG(mode):
        return 'file'
    return 'link' if stat.S_ISLNK(mode) else 'other'


def main_folder(path: str) -> bool:
    """Tell if python runs a file of the folder when it gets the folder as its script.

    Python imports __main__ from the folder, so each entry with that name and any suffix counts, for example
    __main__.py or __main__.pyc. A folder that cannot be listed counts too, because python can still open a
    file in it by its name. A stat error other than a missing entry, such as ELOOP or ENAMETOOLONG, counts
    too, because the check cannot tell what the path names.
    """
    try:
        if entry_kind(path) != 'folder':
            return False
        return any(name.startswith('__main__') for name in os.listdir(path))
    except OSError:
        return True


def unchecked_entry(path: Path, error: OSError) -> str:
    """The reason for a path that the guest cannot stat: the check cannot tell what the path names."""
    return f'{path} cannot be checked ({error.strerror or type(error).__name__})'


def unread_entry(copy: Path, word: str) -> str:
    """The reason that the word can name a guest entry that natedev did not read, or empty. The path starts at
    the copy.

    A word with a slash must have no .. and no symbolic link in a part, and its last part must not exist. A word
    with no slash names one entry of the copy, and a plain folder passes: cargo test runs with a test folder.
    The check uses lstat on each part, so it follows no link outside the read limit. A missing part ends the path;
    each other lstat error is a refusal, because the guest cannot tell what the path names.
    """
    parts = Path(word).parts
    slash = '/' in word
    if slash and '..' in parts:
        return 'the path has ..'
    mode = stat.S_IFDIR
    for end in range(1, len(parts) + 1):
        path = copy.joinpath(*parts[:end])
        try:
            mode = path.lstat().st_mode
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ENOTDIR):
                return ''
            return unchecked_entry(path, error)
        if stat.S_ISLNK(mode):
            return f'{path} is a symbolic link'
    if not slash and stat.S_ISDIR(mode):
        return ''
    return f'the guest has a file at {copy.joinpath(*parts)}'


def command_refusal(copy: Path, argv: Sequence[str], checked: Sequence[str], search_path: str) -> str:
    """The reason that the guest does not start the argv, or empty. The guest runs only a file that natedev
    read, or a tool.

    checked holds the path in the checkout of each word that natedev read a file for; the copy holds its checked
    blob. A later word must name no main_folder, because python runs a __main__ file of that folder, such as
    __main__.py or __main__.pyc. A later word and a word with a slash that natedev did not read must name no
    unread_entry. For a word with no slash, each match on the guest PATH must be a guest_tool, because a wrapper
    can start a later match. A later word also counts each readable match, because bash finds a script name on
    PATH with no execute bit.

    The check reads PATH outside the read limit. The limit /tmp and /dev/shm start empty, so the limit PATH
    finds no file there that the check does not see.
    """
    copy = copy.resolve()
    for index, word in enumerate(argv):
        if index:
            try:
                # entry_kind names a stat error first; main_folder alone takes it as a __main__ folder.
                folder = entry_kind(str(copy / word)) == 'folder'
            except OSError as error:
                return (f'The guest refuses the command word {word}: {unchecked_entry(copy / word, error)}, '
                        + 'and natedev read no file there.')
            if folder and main_folder(str(copy / word)):
                return (f'The guest refuses the command word {word}: python runs a __main__ file of the folder '
                        + f'{copy / word}, and natedev read no file there.')
        if (index or '/' in word) and Path(word).as_posix() not in checked:
            reason = unread_entry(copy, word)
            if reason:
                return f'The guest refuses the command word {word}: {reason}, and natedev read no file there.'
        if '/' in word:
            continue
        for folder in os.get_exec_path({'PATH': search_path}):
            match = copy / folder / word
            if (match.is_file() and (os.access(match, os.X_OK) or (index and os.access(match, os.R_OK)))
                    and not guest_tool(match.resolve(), copy)):
                return (f'The guest refuses the command word {word}: the guest PATH finds {match}, a program file '
                        + 'that natedev did not read; use a file in the checkout.')
    return ''


def home_folder(environment: Mapping[str, str]) -> Path:
    return Path(environment.get('HOME') or Path.home())


def output_folder(checkout: Path, environment: Mapping[str, str]) -> Path:
    """The one cargo build output folder of the run: CARGO_TARGET_DIR when it is set, else the checkout target."""
    return checkout / environment.get('CARGO_TARGET_DIR', 'target')


def bindable(path: Path, home: Path, *, read: bool) -> bool:
    """The limit can bind the path: it exists, it holds no home folder, and no part of it is a link.

    The program can write a read and write folder, so it can put a link there for the next run. A read only
    bind can have a link into /nix/store, because no one can change that file.
    """
    real = Path(os.path.realpath(path))
    return (os.path.lexists(path) and not home.is_relative_to(path)
            and (real == Path(os.path.normpath(path)) or (read and real.is_relative_to(STORE))))


def shown(folder: Path, kept: Sequence[Path]) -> bool:
    """A PATH folder that the limit adds: absolute, not in a system folder, and it holds no temp folder or
    checkout."""
    return (folder.is_absolute() and not any(folder.is_relative_to(system) for system in SYSTEM_FOLDERS)
            and not any(path.is_relative_to(folder) for path in kept))


def unowned(folder: Path) -> Iterator[Path]:
    """Each entry under the folder that root does not own; a link is no entry, because its target is bound or
    hidden on its own. A folder that cannot be read is skipped: the program cannot read it either."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            continue
        try:
            owner = entry.stat(follow_symlinks=False).st_uid
        except OSError:
            continue
        if owner != 0:
            yield Path(entry.path)
        elif entry.is_dir(follow_symlinks=False):
            yield from unowned(Path(entry.path))


def hidden_words() -> list[str]:
    """The bwrap words that hide each entry of a system folder that root does not own: an empty read only folder
    over a folder, /dev/null over a file."""
    words: list[str] = []
    for folder in OWNED_FOLDERS:
        for path in unowned(folder):
            words += (['--tmpfs', str(path), '--remount-ro', str(path)] if path.is_dir()
                      else ['--ro-bind', os.devnull, str(path)])
    return words


def read_paths(environment: Mapping[str, str], kept: Sequence[Path]) -> list[Path]:
    """The paths of the program environment that the run reads: the rustup folder, the cargo bin folder and
    config, and each PATH folder that the limit adds. kept holds the temp folder and the checkout."""
    home = home_folder(environment)
    cargo_home = Path(environment.get('CARGO_HOME') or home / '.cargo')
    folders = [Path(entry) for entry in environment.get('PATH', '').split(':') if entry]
    return [Path(environment.get('RUSTUP_HOME') or home / '.rustup'), cargo_home / 'bin', cargo_home / 'config.toml',
            *(folder for folder in folders if shown(folder, kept))]


def written_paths(checkout: Path, environment: Mapping[str, str]) -> list[Path]:
    """The paths of the program environment that the run reads and writes, the checkout first: the cargo registry
    and git folders, the cargo lock files, and the sccache folder."""
    home = home_folder(environment)
    cargo_home = Path(environment.get('CARGO_HOME') or home / '.cargo')
    return [checkout, cargo_home / 'registry', cargo_home / 'git', *(cargo_home / name for name in CARGO_LOCKS),
            Path(environment.get('SCCACHE_DIR') or home / '.cache' / 'sccache')]


def limit_words(bwrap: str, status_fd: int, temp: Path, checkout: Path, home: Path, read: Iterable[Path],
                write: Iterable[Path]) -> list[str]:
    """The bwrap words that start a program in its checkout under the read limit; the argv comes after them.

    The attempt temp folder is /tmp, and its bind comes first, so a later bind under /tmp stays visible. A read
    and write bind comes after each read only bind, so a read and write folder in a read only folder stays
    writable. Only a path that is bindable with the home folder is bound. bwrap writes its status lines to
    status_fd.
    """
    words = [bwrap, '--json-status-fd', str(status_fd), '--die-with-parent', '--unshare-pid', '--unshare-net',
             '--dev', '/dev', '--proc', '/proc', '--bind', str(temp), str(TEMP)]
    for system in SYSTEM_FOLDERS:
        words += ['--ro-bind-try', str(system), str(system)]
    words += hidden_words()
    for option, paths in (('--ro-bind', read), ('--bind', write)):
        for item in dict.fromkeys(paths):
            if bindable(item, home, read=option == '--ro-bind'):
                words += [option, str(item), str(item)]
    # The root of the limit is not writable, so a write to a folder that is not bound fails at once.
    return [*words, '--remount-ro', '/', '--setenv', 'TMPDIR', str(TEMP), '--setenv', 'SCCACHE_SERVER_UDS',
            str(SCCACHE_SOCKET), '--setenv', 'CARGO_NET_OFFLINE', 'true', '--chdir', str(checkout), '--']


def setup_failed(status: int, status_fd: int) -> bool:
    """bwrap did not set up the limit or did not start the program.

    bwrap then exits with 1 and writes no exit-code line; it writes that line only when the program ends.
    """
    if status != 1:
        return False
    os.set_blocking(status_fd, False)
    data = b''
    try:
        while chunk := os.read(status_fd, 65536):
            data += chunk
    except BlockingIOError:
        pass
    return EXIT_STATUS.search(data) is None


def limited_command(bwrap: str, status_fd: int, tmp: Path, copy: Path, environment: Mapping[str, str], home: Path,
                    attempt: str, argv: Sequence[str]) -> list[str]:
    """The bwrap words that start the argv of a hand made run in the numbered copy under the read limit.

    The program reads and writes the copy, which holds the run home and the build output, the cargo folders and
    the sccache folder of its environment, and the attempt temp folder, which it sees as /tmp. home is the account
    home of the guest: the limit binds no path that holds it. START_SHELL prints the start line in the limit.
    """
    write = [*written_paths(copy, environment), output_folder(copy, environment)]
    words = limit_words(bwrap, status_fd, tmp, copy, home, read_paths(environment, [tmp, copy]), write)
    return [*words, '/bin/sh', '-c', START_SHELL, run_start_line(attempt), *argv]


def refuse_run(reason: str, attempt: str) -> int:
    """Print the reason and the refused line: the guest started nothing."""
    print(reason, file=sys.stderr, flush=True)
    print(run_refused_line(attempt), flush=True)
    return 1


def run_limited(tmp: Path, argv: Sequence[str], copy: Path, environment: dict[str, str], attempt: str) -> int:
    """Run the argv of a hand made run under the read limit, with its output on the stream standard output.

    When bwrap is missing or does not set up the limit, the program does not start, and the guest refuses the run.
    """
    bwrap = shutil.which('bwrap')
    if bwrap is None:
        return refuse_run(BWRAP_MISSING, attempt)
    status_read, status_write = os.pipe()
    try:
        try:
            command = limited_command(bwrap, status_write, tmp, copy, environment, Path.home(), attempt, argv)
            status = subprocess.run(command, cwd=copy, env=environment, stdin=subprocess.DEVNULL, check=False,
                                    pass_fds=(*build_folders.held_build_folder_descriptors(), status_write)).returncode
        finally:
            os.close(status_write)
        if setup_failed(status, status_read):
            return refuse_run(BWRAP_FAILED.format(status=status), attempt)
        return status
    finally:
        os.close(status_read)


def run_guest(request_path: Path) -> int:
    """Take a numbered copy, then run the gate or the argv of the request in it."""
    from scripts.lib import build_slots

    request = guest_request(request_path)
    main = Path.cwd()
    work = request['work'] if 'argv' in request else request['mode']
    with (build_folders.inherit_build_lock(RUN_LOCK_FD),
          build_slots.take_copy(main, request['commit'], showrunner='', unit=request['unit'],
                                work=work, purpose=TARGET) as copy):
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
            if 'argv' in request:
                command = list(request['argv'])
                refusal = command_refusal(copy.path, command, request['checked'], environment['PATH'])
                if refusal:
                    return refuse_run(refusal, request['attempt'])
                return run_limited(tmp, command, copy.path, environment, request['attempt'])
            if request['type_dirs']:
                environment['GATE_STATE_DIR'] = str(runtime / 'gate-state')
            reply = os.dup(1)
            try:
                # The -c entry selects the numbered copy as the Python checkout.
                return run_sandboxed_gate(tmp, gate_entry_command(request['jobs'], Path(__file__).resolve(),
                                                                  request_path, reply),
                                          copy.path, environment, reply)
            finally:
                os.close(reply)


def run_sandboxed_gate(tmp: Path, command: Sequence[str], copy: Path, environment: dict[str, str],
                       reply: int) -> int:
    """Run the gate in the sandbox, with its standard output on standard error.

    Only the gate writes to the stream standard output, through the reply descriptor. Output of
    dbus-run-session or of a service that it starts cannot come before the reply.
    """
    return subprocess.run(session_command(tmp, command), cwd=copy, env=environment, stdin=subprocess.DEVNULL,
                          stdout=sys.stderr.fileno(), check=False,
                          pass_fds=(*build_folders.held_build_folder_descriptors(), reply)).returncode


def session_command(tmp: Path, command: Sequence[str]) -> list[str]:
    """The sandbox command, under dbus-run-session if the guest has it."""
    if shutil.which('dbus-run-session'):
        command = ['dbus-run-session', '--', *command]
    return sandboxed_suite_command(tmp, command)


def gate_entry_command(jobs: int, runner: Path, request_path: Path, reply: int) -> list[str]:
    """Run the runner in the numbered copy with the argv [runner, request, --run-copy, reply descriptor].

    Python gives a -c entry the argv ['-c', *arguments], so the entry removes '-c' before it runs the runner.
    """
    return ['./scripts/lib/py', '-X', f'cpu_count={jobs}',
            '-c', 'import runpy, sys; sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name="__main__")',
            str(runner), str(request_path), RUN_COPY, str(reply)]


def guest_request(path: Path) -> GuestRequest | GuestCommandRequest:
    return cast(GuestRequest | GuestCommandRequest, json.loads(path.read_text(encoding='utf-8')))


def run_copy(request_path: Path, reply: int) -> None:
    """Run the candidate's gate and write one JSON result to the reply descriptor.

    run_sandboxed_gate points standard output at standard error, so only the reply descriptor reaches the stream
    standard output.
    """
    os.set_inheritable(reply, False)
    from scripts.copies import copies
    from scripts.lib import build_folders
    from scripts.production import gate
    from scripts.production.gate_routes import ALL_MODULES, SelectedModules
    from scripts.test_times.gate_record import GateTimeLimit

    repo = Path.cwd()
    request = guest_request(request_path)
    if 'argv' in request:
        raise GuestUnavailable('The gate run got a command request.')
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
    # The configured gate root (/dev/shm, private to the sandbox) keeps test folders outside the copy and the home.
    machine = gate.WholeMachine()
    token = copies.COMMAND_LOCKS.set(descriptors)
    # natedev reads this line: a failure before it lets natedev take the gate.
    print(run_start_line(request['attempt']), file=sys.stderr, flush=True)
    try:
        with build_folders.inherit_build_lock(RUN_LOCK_FD):
            run = gate.run_suites(repo, type_dirs=tuple(map(str, type_dirs)), full=request['full'],
                                  mode=SuiteMode(request['mode']), machine=machine,
                                  selected=ALL_MODULES if selection == 'all' else SelectedModules(tuple(selection)),
                                  module_time_limit_s=request['module_time_limit_s'],
                                  time_limit=GateTimeLimit(request['run_time_limit_seconds']))
    finally:
        copies.COMMAND_LOCKS.reset(token)
    with os.fdopen(reply, 'w', encoding='utf-8') as output:
        print(json.dumps({'attempt': request['attempt'], 'commit': commit, 'tree': tree, 'origin_main': origin_main,
                          'verdict': 'pass' if run.passed else 'fail', 'gate': gate_box.encode_run(run)}),
              file=output)


if __name__ == '__main__':
    if sys.argv[2:3] == [RUN_COPY]:
        run_copy(Path(sys.argv[1]), int(sys.argv[3]))
    else:
        raise SystemExit(run_guest(Path(sys.argv[1])))

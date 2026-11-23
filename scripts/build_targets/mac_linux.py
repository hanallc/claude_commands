"""Run the gate, or one argv of a hand made run, in the Mac's Linux guest.

Wait for proof that the guest run ended.
If the wait expires, fail the route. The guest keeps its run lock until the run ends.
If the gate parent and module supervisor die, a descendant can keep the lock and prevent proof.
Without a signal handler, Python can raise KeyboardInterrupt at any bytecode. An interrupt outside the protected block acts like process death. The guest timeout still ends the run.
"""
from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, redirect_stdout, suppress
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
from typing import Literal, TypedDict, cast, final
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
# The guest prints this line, with the attempt, before it starts the argv of a command request.
RUN_START = 'mac-linux run start'
# The guest prints this line, with the attempt, to stderr when it has taken the run lock.
RUN_LOCKED = 'mac-linux run locked'
# The guest stream exits with this status when the guest took no run lock, so the guest started nothing.
GUEST_NOT_LOCKED_STATUS = 77
# Folders of installed tools, on natedev and on the guest. A program found in one of them is a tool, not a program
# file that the hand made check reads. They are here because the guest runner can read only this module.
TOOL_FOLDERS = (Path('/nix/store'), Path('/usr'), Path('/bin'), Path('/sbin'))
# The first bytes of a compiled program file.
COMPILED_START = b'\x7fELF'
# The tree entry modes of a regular file.
FILE_MODES = ('100644', '100755')
CANCEL_RECORD_PREFIX = 'mac-linux-cancel'


class GuestUnavailable(RuntimeError):
    """The guest cannot return a completed gate."""


class GuestNotLocked(GuestUnavailable):
    """The guest took no run lock, so it started nothing and has no end to prove."""


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
                          gate_job: StepJob | GateJobNotRecorded) -> AbstractContextManager[None]:
    """Take the Mac repository claim for the guest route."""
    from scripts.build_queue import mac_run

    return mac_run.held(repo, 'mac', work, gate_job)


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
        return GuestNotStarted(f'{TARGET}: guest unreachable during retry backoff')
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


class GuestNoAnswer(GuestUnavailable):
    """The guest did not answer before it armed a run, so nothing ran there."""


@dataclass(frozen=True)
class GuestNotStarted:
    """The guest took no run: the Mac refused the claim, or the guest gave no answer before the arm."""

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
STREAM_SOURCE = '''
prefix=$1
base=$2
header=$3
count=$4
shift 4
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
if [ -z "$count" ]; then
    { cat && bundle; } | "$@"
    exit
fi
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


def bundle_send(repo: Path, source: CheckoutSource, bundle: FullBundle | DeltaBundle, prefix: str) -> BundleSend:
    if isinstance(bundle, DeltaBundle):
        return BundleSend(bundle.base, empty_delta_header(repo, source, bundle, prefix), BundleForm.DELTA, BundleReason.DELTA)
    return BundleSend('', '', BundleForm.FIRST, bundle.reason)


def stream_arguments(settings: GuestSettings, source: CheckoutSource, bootstrap_bytes: int, armed: ArmedGuestAttempt,
                     prefix: str, send: BundleSend, *, count: bool) -> list[str]:
    """The local command that sends the bootstrap and the bundle to the armed guest run."""
    command = settings.command(run_shell(settings, source, bootstrap_bytes, armed))
    return ['bash', '-o', 'pipefail', '-c', STREAM_SOURCE, TARGET, prefix, send.base, send.header,
            'count' if count else '', *command]


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
                            raise GuestNoAnswer(f'the guest did not arm the run: {error}') from error
                        arguments = stream_arguments(settings, source, len(bootstrap), armed, prefix, send, count=True)
                        stream = stream_guest_bundle(repo, arguments, bootstrap, settings.transfer_timeout_seconds)
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
    diagnostics = stream.stderr.decode(errors='replace') if stream is not None and stream.returncode else ''
    detail = ' '.join(diagnostics.split())[-1200:]
    if not end.proven:
        error = end.unreachable()
        if detail:
            error.add_note(detail)
        raise error

    cleanup_detail = end.cleanup_detail()
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
    return GuestReply(result.decode('utf-8'), int(count) - len(bootstrap), send.form, send.reason)


def guest_bundle(repo: Path, settings: GuestSettings) -> FullBundle | DeltaBundle:
    """Probe the guest main commit, and leave out the history that the guest has.

    A failed probe records the retry backoff of the guest.
    """
    try:
        guest = read_guest_main(settings)
    except (GuestUnavailable, OSError, subprocess.SubprocessError) as error:
        from scripts.mac_test import offload

        no_answer = GuestNoAnswer(f'the guest main probe failed: {error}')
        try:
            delay = conf.read('mac_test').number('unreachable_backoff_s')
            offload.write_backoff(time() + delay, TARGET, strict=True)
        except (conf.Unusable, OSError) as backoff_error:
            no_answer.add_note(f'Cannot record the {TARGET} backoff: {backoff_error}')
        raise no_answer from error
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
        the output pipe. After GuestNotLocked, finish proves no end. The process has one output pipe for its
        standard output and standard error.
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
        arguments = stream_arguments(self.settings, source, len(data), self.armed, prefix, send, count=False)
        with bootstrap.open('rb') as stdin:
            self.process = subprocess.Popen(arguments, cwd=repo, stdin=stdin, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, start_new_session=True)
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
        """Prove the end of an armed run, then remove the bundle refs.

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

    Output before the start line, other than the lock line, goes to stderr. A stream that ends before the start
    line raises GuestUnavailable with that output. When it ends before the lock line with GUEST_NOT_LOCKED_STATUS,
    the error is GuestNotLocked. A stream that ends with another status can come from a lost SSH link after
    the guest took the lock, so it needs the end proof.
    """
    assert process.stdout is not None
    descriptor = process.stdout.fileno()
    start = run_start_line(attempt).encode('utf-8') + b'\n'
    locked_line = run_locked_line(attempt).encode('utf-8') + b'\n'
    locked = False
    before = bytearray()
    line = bytearray()
    while True:
        byte = os.read(descriptor, 1)
        if not byte:
            status = process.wait()
            text = (before + line).decode('utf-8', errors='replace').strip()
            error = GuestNotLocked if status == GUEST_NOT_LOCKED_STATUS and not locked else GuestUnavailable
            raise error(text or f'the guest stream exited {status} before the argv started')
        line += byte
        if byte == b'\n':
            if bytes(line) == start:
                break
            if bytes(line) == locked_line:
                locked = True
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
          type_dirs: Sequence[str] = ()) -> GateRun | GuestNotStarted | str:
    """Return the guest gate result, a run the guest did not take, or the reason the guest route failed."""
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
            with mac_linux_reservation(repo, mode, gate_job):
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
        return GuestNotStarted(f'{TARGET}: not started: {error}')
    except GuestNoAnswer as error:
        notes = cast(list[str], getattr(error, '__notes__', []))
        return GuestNotStarted(f'{TARGET}: not started: ' + ' '.join((str(error), *notes)))
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


def main_folder(path: str) -> bool:
    """Tell if python runs a file of the folder when it gets the folder as its script.

    Python imports __main__ from the folder, so each entry with that name and any suffix counts, for example
    __main__.py or __main__.pyc. A folder that cannot be listed counts too, because python can still open a
    file in it by its name.
    """
    if not os.path.isdir(path):
        return False
    try:
        return any(name.startswith('__main__') for name in os.listdir(path))
    except OSError:
        return True


def unread_entry(copy: Path, word: str) -> str:
    """The reason that the word can name a guest entry that natedev did not read, or empty. The path starts at
    the copy.

    A word with a slash must have no .. and no symbolic link in a part, and its last part must not exist. A word
    with no slash names one entry of the copy, and a plain folder passes: cargo test runs with a test folder.
    The check uses lstat on each part, so it follows no link outside the sandbox. A missing part ends the path;
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
            return f'the guest cannot read {path}: {error.strerror}'
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

    The check reads PATH outside the sandbox. The sandbox /tmp and /dev/shm start empty, so the sandbox PATH
    finds no file there that the check does not see.
    """
    copy = copy.resolve()
    for index, word in enumerate(argv):
        if index and main_folder(str(copy / word)):
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
                    print(refusal, file=sys.stderr, flush=True)
                    return 1
            else:
                if request['type_dirs']:
                    environment['GATE_STATE_DIR'] = str(runtime / 'gate-state')
                # The -c entry selects the numbered copy as the Python checkout.
                command = ['./scripts/lib/py', '-X', f'cpu_count={request["jobs"]}',
                           '-c', 'import runpy, sys; runpy.run_path(sys.argv[1], run_name="__main__")',
                           str(Path(__file__).resolve()), str(request_path), '--run-copy']
            if shutil.which('dbus-run-session'):
                command[:0] = ['dbus-run-session', '--']
            if 'argv' in request:
                print(run_start_line(request['attempt']), flush=True)
            return subprocess.run(sandboxed_suite_command(tmp, command), cwd=copy.path, env=environment,
                                  stdin=subprocess.DEVNULL, check=False,
                                  pass_fds=build_folders.held_build_folder_descriptors()).returncode


def guest_request(path: Path) -> GuestRequest | GuestCommandRequest:
    return cast(GuestRequest | GuestCommandRequest, json.loads(path.read_text(encoding='utf-8')))


def run_copy(request_path: Path) -> None:
    """Run the candidate's gate and write one JSON result to standard output."""
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

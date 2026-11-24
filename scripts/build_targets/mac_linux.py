"""Run the gate, or one argv of a hand made run, in the Mac's Linux guest.

Wait for proof that the guest run ended.
If the wait expires, fail the route. The guest keeps its run lock until the run ends.
If the gate parent and module supervisor die, a descendant can keep the lock and prevent proof.
Without a signal handler, Python can raise KeyboardInterrupt at any bytecode. An interrupt outside the protected block acts like process death.
When this process ends by any signal, the link to the guest ends, and the guest ends the run. The guest timeout still ends the run.
"""
from __future__ import annotations

from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future
from contextlib import AbstractContextManager, ExitStack, contextmanager, suppress
from dataclasses import dataclass, field, replace
from enum import StrEnum
import errno
import fcntl
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import os
import re
from select import PIPE_BUF
from selectors import DefaultSelector, EVENT_READ, EVENT_WRITE
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
from threading import Thread
from time import monotonic, sleep, time
from typing import TYPE_CHECKING, Literal, TypedDict, cast, final
from urllib.parse import urlsplit
from uuid import uuid4

from scripts.aws_target import gate_box, ledger
from scripts.build_queue.admission_jobs import GateJobNotRecorded, StepJob
from scripts.build_targets import target_config
from scripts.lib import atomic_file, build_folders, conf, process_group, process_start
from scripts.mac_test import claim_state, eligibility
from scripts.production.gate_modules import GateRefused, discover_test_modules
from scripts.production.gate_routes import (
    AllModules, GateJobKind, GateResultUnknown, GateRun, GuestGateBlocked, ModuleCoverage, SuiteMode, TreeSelection,
)
from scripts.test_times.gate_record import TYPE_CHECK_JOB

if TYPE_CHECKING:
    from scripts.build_queue.admission_protocol import DispatchStatus, ExecutionReport, Reply
    from scripts.build_queue.admission_wire import PlacementStatusRequest
    from scripts.build_queue.placement import AttemptCompleted, NoTerminalReport, Pending, PlacementAssignment, ProviderClaimReference, Settled


TARGET = eligibility.MAC_LINUX_TARGET
GUEST_CLAIM_TARGET = eligibility.MAC_LINUX_CLAIM_TARGET
ORIGIN_MAIN = 'refs/remotes/origin/main'
RUNNER_PATH = 'scripts/build_targets/mac_linux.py'
BOOTSTRAP_DIR = '.git/mac-linux'
SSH_CONNECT_TIMEOUT_SECONDS = 8
CONNECT_ATTEMPTS = 3
# Give the guest port forward time to accept a new connection.
CONNECT_RETRY_SECONDS = 1
INNER_CONNECT_READY = 'mac-linux inner SSH ready'
INNER_CONNECT_MARKER = 'mac-linux inner SSH connect failed'
INNER_CONNECT_STATUS = 254
GUEST_ACCEPTED = 'mac-linux command accepted'
INNER_CONNECT_ERROR = re.compile(
    r'ssh: connect to host [^\s]+ port [0-9]+: Connection (?:reset by peer|refused)\r?\n?')
SSH_OPTIONS = ('-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ForwardAgent=no')
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
# The guest keeps placed arm and refusal records after the armed deadline.
PLACED_GUEST_ATTEMPT_PREFIX = 'gate-'
FULL_BUNDLE_RETRY_SUFFIX = '-full'
# The guest prints this line, with the attempt, before it starts the argv of a command request.
RUN_START = 'mac-linux run start'
GUEST_PROGRESS = 'mac-linux progress '
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


class GuestArmNotSent(GuestUnavailable):
    """The guest received no arm command."""


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
class GuestCheckoutIdentity:
    """The guest machine and its physical checkout path."""

    machine_id: str
    checkout: str


@dataclass(frozen=True)
class GuestCheckoutUnrecorded:
    """The call has no saved guest checkout identity."""


@dataclass(frozen=True)
class GuestSettings:
    ssh_hops: tuple[str, str]
    guest_user: str
    jobs: int
    run_time_limit_seconds: int
    unreachable_wait_seconds: int
    checkout_identity: GuestCheckoutIdentity | GuestCheckoutUnrecorded = GuestCheckoutUnrecorded()

    @property
    def guest_timeout_seconds(self) -> int:
        return self.run_time_limit_seconds + TRANSFER_ALLOWANCE_SECONDS

    @property
    def transfer_timeout_seconds(self) -> int:
        return self.guest_timeout_seconds + TRANSFER_ALLOWANCE_SECONDS

    def command(self, shell: str, *, diagnose_connect: bool = True, budget_inner_connect: bool = False,
                connect_timeout: int = SSH_CONNECT_TIMEOUT_SECONDS) -> tuple[str, ...]:
        mac, guest = self.ssh_hops
        options = (*SSH_OPTIONS, *eligibility.server_alive_options(self.unreachable_wait_seconds))
        fixed_timeout = f'ConnectTimeout={connect_timeout}'
        inner_timeout = '"ConnectTimeout=$guest_connect_timeout"' if budget_inner_connect else fixed_timeout
        guest_command = shlex.join((*options, '-l', self.guest_user, guest, shlex.join(('bash', '-c', shell))))
        guest_arguments = f'-o {inner_timeout} {guest_command}'
        inner = f'ssh {guest_arguments}'
        if diagnose_connect:
            # Only the SSH client writes this log. The shell reserves one status for a verified connect failure.
            inner = f'''log=$(mktemp "${{TMPDIR:-/tmp}}/mac-linux-ssh.XXXXXXXXXX") || exit 1
trap 'rm -f -- "$log"' EXIT
LC_ALL=C ssh -E "$log" {guest_arguments}
guest_ssh_status=$?
if [ "$guest_ssh_status" -eq 255 ] && LC_ALL=C awk '
    {{ sub(/\\r$/, "") }}
    NR == 1 && /^ssh: connect to host [^[:space:]]+ port [0-9]+: Connection (reset by peer|refused)$/ {{ found = 1 }}
    END {{ exit !(NR == 1 && found) }}
' "$log"; then
    printf '%s\\n' {shlex.quote(INNER_CONNECT_MARKER)} >&2
    guest_ssh_status={INNER_CONNECT_STATUS}
elif [ "$guest_ssh_status" -eq {INNER_CONNECT_STATUS} ]; then
    printf '%s\\n' 'The guest SSH command exited {INNER_CONNECT_STATUS}.' >&2
    guest_ssh_status=255
fi
cat "$log" >&2
exit "$guest_ssh_status"'''
        if budget_inner_connect:
            inner = f'''printf '%s\\n' {shlex.quote(INNER_CONNECT_READY)}
IFS= read -r guest_connect_timeout || exit 1
case "$guest_connect_timeout" in
    ""|0*|*[!0-9]*) exit 1 ;;
esac
{inner}'''
        return ('ssh', '-o', fixed_timeout, *options, mac, inner)


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


def consume_launch_permission(launch: GuestLaunchRecovery,
                              assignment: PlacementAssignment) -> ProviderClaimReference:
    """Consume permission after the gate syncs its launch record.

    The gate process is the holder, so the queue keeps the guest assignment current until the gate ends.
    If the reply is lost, the gate stops before it sends the arm command.
    """
    from scripts.build_queue import admission_client, admission_protocol as wire, placement
    from scripts.buildlog import store

    gate_holder, = launch.holders
    destination = assignment.destination
    reference = placement.ProviderClaimReference(destination.provider, destination.target, destination.scope,
                                                 launch.claim, assignment.episode, assignment.attempt)
    holder = placement.ClaimHolderIdentity(store.host_name(), gate_holder.pid, gate_holder.start)
    try:
        permission = admission_client.consume_permission(
            wire.ConsumePermission(admission_client.request_id(), assignment, reference, holder, guest_proof_required=True))
    except wire.AdmissionUnavailable as error:
        raise GuestPermissionUnknown(f'the launch permission is unavailable: {error}', reference) from error
    if isinstance(permission, wire.DispatchStatus):
        reason = f'the queue refused the launch permission: {permission.reason}'
        if isinstance(permission.permission, placement.PermissionConsumed):
            raise GuestPermissionUnknown(reason, reference)
        raise GuestTookNoRun(reason)
    return reference


@dataclass
class GuestGateDispatch:
    """The guest gate holds the queue's launch permission for this claim."""

    assignment: PlacementAssignment
    claim: ProviderClaimReference
    started: ArmedGuestAttempt | None = None
    settled: bool = False
    arm_requested: bool = False


class GuestRunRequest(TypedDict):
    """The fields of each guest request: the attempt, the commit to copy and the unit that owns the copy."""
    attempt: str
    commit: str
    tree: str
    origin_main: str
    runner_commit: str
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
    runner_commit: str

    def bundle_tips(self) -> dict[str, str]:
        return {'candidate': self.commit, 'main': self.origin_main, 'runner': self.runner_commit}


@dataclass(frozen=True)
class CandidateSource(CheckoutSource):
    modules: frozenset[str]
    coverage: ModuleCoverage


def check_repository(repo: Path) -> None:
    """Refuse a checkout of a repository other than claude; the guest holds only the claude checkout."""
    repository = build_folders.repo_word(repo)
    if repository != 'claude':
        raise GuestRepositoryRefused(repository)


def runner_commit(repo: Path) -> str:
    """Find the clean runner commit and make its objects available to the bundle."""
    from scripts.production.gate import checked_git, git

    runner_folder = Path(__file__).resolve().parent
    try:
        top, _, commit = checked_git(runner_folder, 'rev-parse', '--show-toplevel', 'HEAD').rpartition('\n')
        root = Path(top)
        entries = checked_git(root, 'ls-files', '-v', '-z', '--cached', '--others', '--exclude-standard', '--', 'scripts/')
        tags = {entry[0] for entry in entries.split('\0') if entry}
        if any(tag == 'S' or tag.islower() for tag in tags):
            raise GuestTookNoRun(f'the guest runner scripts have assume-unchanged or skip-worktree flags in {root}')
        if ('?' in tags
                or checked_git(root, 'diff', '--cached', '--name-only', commit, '--', 'scripts/')
                or checked_git(root, 'diff', '--name-only', '--', 'scripts/')):
            raise GuestTookNoRun(f'the guest runner scripts have uncommitted changes in {root}')
        if git(repo, 'cat-file', '-e', f'{commit}^{{commit}}').returncode:
            _ = checked_git(repo, 'fetch', '--no-tags', '--no-write-fetch-head', str(root), commit)
    except (GateRefused, OSError, subprocess.SubprocessError) as error:
        raise GuestTookNoRun(f'cannot prepare the guest runner commit: {error}') from error
    return commit


def checkout_source(repo: Path) -> CheckoutSource:
    from scripts.aws_target import cloud_gate
    from scripts.production.gate import checked_git

    check_repository(repo)
    runner = runner_commit(repo)
    head = checked_git(repo, 'rev-parse', 'HEAD')
    origin_main = checked_git(repo, 'rev-parse', '--verify', f'{ORIGIN_MAIN}^{{commit}}')
    tree, dirty = cloud_gate.working_tree(repo)
    origin_url = checked_git(repo, 'remote', 'get-url', 'origin')
    origin = urlsplit(origin_url)
    if origin.password or origin.query or ('://' in origin_url and origin.username):
        raise GuestUnavailable('the origin URL must contain no credential')
    commit = checked_git(repo, 'commit-tree', tree, '-p', head, '-m', 'Mac Linux gate snapshot') if dirty else head
    return CheckoutSource(head, commit, tree, origin_main, origin_url, runner)


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
                           source.runner_commit, modules, coverage)


def runner_bootstrap(request: GuestRequest | GuestCommandRequest) -> bytes:
    manifest = json.dumps(request).encode('utf-8')
    record = tarfile.TarInfo('request.json')
    record.size = len(manifest)
    with BytesIO() as bootstrap:
        with tarfile.open(fileobj=bootstrap, mode='w') as archive:
            archive.addfile(record, BytesIO(manifest))
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

GUEST_CHECKOUT_IDENTITY = '''
machine_id=$(cat /etc/machine-id)
physical_home=$(CDPATH= cd -P -- "${checkout%/*}" >/dev/null && pwd -P)
physical_checkout="${physical_home%/}/.claude"'''


def _checkout_probe_output(stdout: str, stderr: str) -> str:
    return f'stdout={repr(stdout[:300])[:300]} stderr={repr(stderr[:300])[:300]}'


def read_guest_checkout(settings: GuestSettings, repo: Path) -> GuestCheckoutIdentity:
    shell = guest_shell(settings) + GUEST_CHECKOUT_IDENTITY + '''
printf 'checkout %s %s\\n' "$machine_id" "$physical_checkout"'''
    diagnostics: list[str] = []
    reason = 'the guest checkout probe returned an invalid identity'
    for _ in range(2):
        try:
            reply = subprocess.run(settings.command(shell), stdin=subprocess.DEVNULL, capture_output=True,
                                   timeout=GUEST_REF_TIMEOUT_SECONDS, check=False, env=guest_ssh_environment())
        except subprocess.TimeoutExpired as error:
            if not diagnostics:
                raise
            partial = _checkout_probe_output((error.stdout or b'').decode(errors='replace'),
                                             (error.stderr or b'').decode(errors='replace'))
            raise GuestUnavailable(f'{reason} in natedev checkout {str(repo)!r}. '
                                   + f'{diagnostics[0]} | retry timed out {partial}') from error
        stdout = reply.stdout.decode(errors='replace')
        identity = re.fullmatch(r'checkout ([0-9a-f]{32}) (/[^\r\n\0]*)\r?\n', stdout)
        if not reply.returncode and identity is not None:
            return GuestCheckoutIdentity(identity[1], identity[2])

        stderr = reply.stderr.decode(errors='replace')
        diagnostics.append(f'exit {reply.returncode} ' + _checkout_probe_output(stdout, stderr))
        if reply.returncode:
            reason = 'the guest checkout probe exited with an error'
            break
    raise GuestUnavailable(f'{reason} in natedev checkout {str(repo)!r}. ' + ' | '.join(diagnostics))


def guest_checkout_guard(settings: GuestSettings) -> str:
    identity = settings.checkout_identity
    shell = guest_shell(settings)
    if isinstance(identity, GuestCheckoutUnrecorded):
        return shell
    return shell + GUEST_CHECKOUT_IDENTITY + f'''
if [ "$machine_id" != {shlex.quote(identity.machine_id)} ]; then
    printf 'A different guest answered the SSH request.\\n' >&2
    exit {GUEST_END_PENDING_STATUS}
fi
if [ "$physical_checkout" != {shlex.quote(identity.checkout)} ]; then
    printf 'The guest checkout differs from the saved checkout.\\n' >&2
    exit {GUEST_END_PENDING_STATUS}
fi'''


@dataclass(frozen=True)
class EmptyGuestMain:
    """The guest does not have a main commit."""


@dataclass(frozen=True)
class GuestMain:
    """Use the guest main commit as a bundle prerequisite."""

    commit: str


def guest_ssh_environment() -> dict[str, str]:
    environment = dict(os.environ)
    _ = environment.pop('BUILDLOG_PLACEMENT_SIDECAR', None)
    return environment


def inner_connect_failure[Output: (bytes, str)](
    reply: subprocess.CompletedProcess[Output], *, bundle_stream: bool = False,
) -> str:
    if reply.returncode == INNER_CONNECT_STATUS:
        if reply.stdout:
            return ''
    elif (not bundle_stream or reply.returncode != 0
          or reply.stdout not in (b'', b'\n\n', '', '\n\n')):
        return ''
    stderr = reply.stderr.decode(errors='replace') if isinstance(reply.stderr, bytes) else reply.stderr
    if bundle_stream:
        # Git can write this line when SSH closes the local bundle pipe.
        stderr = ''.join(line for line in stderr.splitlines(keepends=True) if line != 'error: pack-objects died\n')
    marker = INNER_CONNECT_MARKER + '\n'
    diagnostic = stderr.removeprefix(marker)
    return diagnostic.strip() if stderr != diagnostic and INNER_CONNECT_ERROR.fullmatch(diagnostic) else ''


def retry_guest_connect[Output: (bytes, str)](
    run: Callable[[float], subprocess.CompletedProcess[Output]], deadline: float, *, bundle_stream: bool = False,
) -> subprocess.CompletedProcess[Output]:
    """Retry only when the inner SSH client could not connect."""
    diagnostic = ''
    attempts = 0
    while (remaining := deadline - monotonic()) > 0:
        reply = run(remaining)
        attempts += 1
        diagnostic = inner_connect_failure(reply, bundle_stream=bundle_stream)
        if not diagnostic:
            return reply
        if attempts == CONNECT_ATTEMPTS or deadline - monotonic() <= CONNECT_RETRY_SECONDS:
            break
        sleep(CONNECT_RETRY_SECONDS)
    if not attempts:
        raise GuestConnectAttemptsFailed('the guest connect deadline expired before SSH')
    suffix = 'attempt' if attempts == 1 else 'attempts'
    raise GuestConnectAttemptsFailed(f'{diagnostic} after {attempts} connect {suffix}')


def run_guest_ssh(command: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace',
                          timeout=timeout, check=False, env=guest_ssh_environment())


def read_guest_main(settings: GuestSettings) -> GuestMain | EmptyGuestMain:
    shell = guest_shell(settings) + '''
if ! command -v bwrap >/dev/null 2>&1; then
    printf 'bwrap is absent from the guest PATH.\\n' >&2
    exit 127
fi
if [ -f "$checkout/.git/HEAD" ]; then
    git -C "$checkout" rev-parse --verify --quiet refs/heads/main || test "$?" -eq 1
fi'''
    command = settings.command(shell)
    reply = retry_guest_connect(lambda remaining: run_guest_ssh(command, min(GUEST_REF_TIMEOUT_SECONDS, remaining)),
                                monotonic() + settings.unreachable_wait_seconds)
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
    """The gate saw a refusal or received no start marker before the stream failed."""


class GuestPermissionUnknown(GuestTookNoRun):
    """The queue may hold consumed permission without giving the gate launch authorization."""

    def __init__(self, reason: str, claim: ProviderClaimReference) -> None:
        super().__init__(reason)
        self.claim: ProviderClaimReference = claim


class GuestConnectAttemptsFailed(GuestTookNoRun):
    """The inner SSH connection failed before the guest received a command."""


@dataclass(frozen=True)
class GuestNotStarted:
    """The gate can request a replacement after any required settlement.

    The reason names only the cause; the gate says that the guest took no run.
    """

    reason: str
    settlement_required: bool = False
    retry_before_settlement: bool = False


class GuestEndUnproven(GuestUnavailable):
    """The proof wait expired for an armed guest attempt."""

    def __init__(self, message: str, armed: ArmedGuestAttempt, *, retry_before_settlement: bool = False) -> None:
        super().__init__(message)
        self.armed: ArmedGuestAttempt = armed
        self.retry_before_settlement: bool = retry_before_settlement


GUEST_ARM_RECORDS = '''\
import fcntl
import os
import stat
import sys
import time

mode, prefix, cancel_prefix, attempt, placed_attempt, full_suffix, window, lock_name, descriptor = sys.argv[1:]
if not attempt or any(character not in '0123456789abcdefghijklmnopqrstuvwxyz-' for character in attempt):
    sys.exit('The guest attempt ID is invalid.')
git = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

def sync_checkout():
    os.fsync(git)
    for path in ('.', '..'):
        parent = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(parent)
        os.close(parent)

if placed_attempt:
    lock = int(descriptor)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit('The guest run lock is held.')
    path_stat = os.stat(lock_name, dir_fd=git, follow_symlinks=False)
    if not os.path.samestat(os.fstat(lock), path_stat):
        sys.exit('The guest run lock path changed.')
    refusal = f'{cancel_prefix}.refused.{placed_attempt}'
    if mode == 'recover':
        attempts = (placed_attempt, placed_attempt + full_suffix)
        armed = False
        for name in os.listdir(git):
            if name.startswith(prefix + '.') and name.rpartition('.')[2] in attempts:
                if not stat.S_ISREG(os.stat(name, dir_fd=git, follow_symlinks=False).st_mode):
                    sys.exit('The guest armed record must be a regular file.')
                armed = True
        try:
            os.close(os.open(refusal, flags, 0o600, dir_fd=git))
        except FileExistsError:
            if not stat.S_ISREG(os.stat(refusal, dir_fd=git, follow_symlinks=False).st_mode):
                sys.exit('The guest refusal record must be a regular file.')
        sync_checkout()
        print('ended' if armed else 'not started', flush=True)
        sys.exit(0)
    try:
        os.stat(refusal, dir_fd=git, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        sys.exit('The guest attempt was refused.')
with open('/etc/machine-id', encoding='ascii') as identity:
    machine = identity.read().rstrip('\\n')
if len(machine) != 32 or any(character not in '0123456789abcdef' for character in machine):
    sys.exit('The guest machine ID is invalid.')
with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as identity:
    boot = identity.read().strip()
deadline = time.clock_gettime_ns(time.CLOCK_BOOTTIME) + int(window) * 10**9
arm_prefix = f'{prefix}.placed' if placed_attempt else prefix
record = f'{arm_prefix}.{boot}.{deadline}.{attempt}'
os.close(os.open(record, flags, 0o600, dir_fd=git))
if placed_attempt:
    sync_checkout()
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


def guest_arm_command(settings: GuestSettings, attempt: str, mode: Literal['arm', 'recover']) -> tuple[str, ...]:
    placed_attempt = (attempt.removesuffix(FULL_BUNDLE_RETRY_SUFFIX)
                      if attempt.startswith(PLACED_GUEST_ATTEMPT_PREFIX) else '')
    # Allow time for the arm reply, the connection, and the guest lock.
    window = min(GUEST_REF_TIMEOUT_SECONDS, settings.unreachable_wait_seconds)
    window += settings.unreachable_wait_seconds + SSH_CONNECT_TIMEOUT_SECONDS
    records = shlex.join(('python3', '-I', '-S', '-c', GUEST_ARM_RECORDS, mode, ARM_RECORD_PREFIX,
                          CANCEL_RECORD_PREFIX, attempt, placed_attempt, FULL_BUNDLE_RETRY_SUFFIX,
                          str(window), Path(RUN_LOCK_PATH).name, str(RUN_LOCK_FD)))
    command = open_run_lock_command(RUN_LOCK_FD, f'exec {records}') if placed_attempt else records
    return settings.command(guest_checkout_guard(settings) + GUEST_CHECKOUT_SETUP + f'\nexec {command}')


def arm_guest(settings: GuestSettings, attempt: str) -> ArmedGuestAttempt:
    try:
        command = guest_arm_command(settings, attempt, 'arm')
    except (OSError, ValueError) as error:
        raise GuestArmNotSent(str(error)) from error

    def send(remaining: float) -> subprocess.CompletedProcess[bytes]:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True, env=guest_ssh_environment())
        except (OSError, ValueError) as error:
            raise GuestArmNotSent(str(error)) from error
        with process:
            try:
                output, errors = process.communicate(timeout=min(GUEST_REF_TIMEOUT_SECONDS, remaining))
            except BaseException:
                stop_ssh_process(process)
                raise
        return subprocess.CompletedProcess(command, process.returncode, output, errors)

    response = retry_guest_connect(send, monotonic() + settings.unreachable_wait_seconds)
    if response.returncode:
        diagnostic = response.stderr.decode(errors='replace').strip() or 'no diagnostic'
        raise GuestUnavailable(f'guest arm exited {response.returncode}: {diagnostic}')
    reply = re.fullmatch(rb'armed ([0-9a-f]{32}) ([0-9a-f-]{36}) ([0-9]{1,20}) (/[^\n\0]*)\n', response.stdout)
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

mode, arm_prefix, cancel_prefix, attempt, boot, deadline, lock_name, descriptor, placed_prefix, full_suffix, *arguments = sys.argv[1:]
with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as identity:
    boot_id = identity.read().strip()
git = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
arm_kind = f'{arm_prefix}.placed' if attempt.startswith(placed_prefix) else arm_prefix
arm = f'{arm_kind}.{boot}.{deadline}.{attempt}'
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
if attempt.startswith(placed_prefix):
    refusal = f'{cancel_prefix}.refused.{attempt.removesuffix(full_suffix)}'
    try:
        os.stat(refusal, dir_fd=git, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        sys.exit('The guest attempt was refused.')
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
                       armed.attempt, armed.boot_id, str(armed.deadline_ns), Path(RUN_LOCK_PATH).name, str(descriptor),
                       PLACED_GUEST_ATTEMPT_PREFIX, FULL_BUNDLE_RETRY_SUFFIX))


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
bootstrap="$PWD/{BOOTSTRAP_DIR}"
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
git archive {shlex.quote(source.runner_commit)} scripts config | tar -xf - -C "$bootstrap"
umask 022
"$bootstrap/scripts/lib/py" "$bootstrap/{RUNNER_PATH}" "$bootstrap/request.json"'''
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
    if ! {cancel} || ! rm -rf -- {BOOTSTRAP_DIR}; then
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
    """The guest ended the attempt under the run lock."""


@dataclass(frozen=True)
class GuestEndPending:
    detail: str


@dataclass(frozen=True)
class GuestEndConnectPending(GuestEndPending):
    """The inner SSH client could not connect for this proof request."""


@dataclass(frozen=True)
class GuestNotStartedProven:
    """The guest received no arm command or recorded a refusal before it armed."""


def recover_guest_launch(settings: GuestSettings, attempt: str) -> GuestEndProven | GuestNotStartedProven | GuestEndPending:
    command = guest_arm_command(settings, attempt, 'recover')
    try:
        reply = run_guest_ssh(command, GUEST_CLEANUP_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        return GuestEndPending(str(error) or type(error).__name__)
    if reply.returncode == 0 and reply.stdout == 'not started\n':
        return GuestNotStartedProven()
    if reply.returncode == 0 and reply.stdout == 'ended\n':
        return GuestEndProven()
    return GuestEndPending(reply.stderr.strip() or f'guest recovery exited {reply.returncode}')


def prove_guest_end(settings: GuestSettings, armed: ArmedGuestAttempt, *,
                    proof_deadline: float = math.inf) -> GuestEndProven | GuestEndPending:
    if settings.guest_user != armed.guest_user:
        # The run lock lives in the armed account's checkout; another account cannot prove its end.
        return GuestEndPending(f'The guest account changed from {armed.guest_user} to {settings.guest_user}.')
    try:
        command = settings.command(cleanup_shell(settings, armed))
        now = monotonic()
        if now >= proof_deadline:
            return GuestEndPending('The proof deadline expired before cleanup SSH.')
        reply = run_guest_ssh(command, min(GUEST_CLEANUP_TIMEOUT_SECONDS, settings.unreachable_wait_seconds,
                                           proof_deadline - now))
    except (OSError, subprocess.TimeoutExpired) as error:
        return GuestEndPending(str(error) or type(error).__name__)
    if diagnostic := inner_connect_failure(reply):
        return GuestEndConnectPending(diagnostic)
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
    connect_attempts: int = 0

    def run(self) -> None:
        while not self.proven:
            now = monotonic()
            if self.deadline is None:
                self.deadline = now + self.settings.unreachable_wait_seconds
            remaining = self.deadline - now
            if remaining <= 0:
                return
            if self.retry:
                short_retry = 0 < self.connect_attempts < CONNECT_ATTEMPTS
                pause = CONNECT_RETRY_SECONDS if short_retry else SSH_CONNECT_TIMEOUT_SECONDS
                sleep(min(pause, remaining))
                if monotonic() >= self.deadline:
                    return
                if not short_retry:
                    self.connect_attempts = 0
            self.retry = True
            self.connect_attempts += 1
            proof = prove_guest_end(self.settings, self.armed, proof_deadline=self.deadline)
            if not isinstance(proof, GuestEndConnectPending):
                self.connect_attempts = 0
            if monotonic() >= self.deadline:
                return
            if isinstance(proof, GuestEndProven):
                self.proven = True
            elif not self.first_failure:
                self.first_failure = proof.detail

    def unreachable(self, *, retry_before_settlement: bool = False) -> GuestEndUnproven:
        guest = self.settings.ssh_hops[-1]
        error = GuestEndUnproven(f'guest unreachable: {guest} did not confirm the run ended '
                                 + f'within {self.settings.unreachable_wait_seconds} seconds ({target_config.UNREACHABLE_WAIT_KEY})',
                                 self.armed, retry_before_settlement=retry_before_settlement)
        if self.first_failure:
            error.add_note(f'First cleanup failure: {self.first_failure}')
        return error

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
    """Send the complete Git history for the candidate, origin/main, and runner."""

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
    tips = source.bundle_tips()
    active_error: BaseException | None = None
    try:
        for name, commit in tips.items():
            _ = checked_git(repo, 'update-ref', f'{prefix}/{name}', commit)
        yield prefix
    except BaseException as error:
        active_error = error
        raise
    finally:
        failure = active_error
        for tip in tips:
            ref = f'{prefix}/{tip}'
            try:
                _ = checked_git(repo, 'update-ref', '-d', ref)
            except BaseException as error:
                if failure is None:
                    failure = error
                elif (isinstance(error, Exception) or isinstance(failure, GuestEndUnproven)
                      or not isinstance(failure, Exception)):
                    failure.add_note(f'Cannot remove bundle ref {ref}: {error}')
                    if isinstance(failure, GuestEndUnproven):
                        failure.retry_before_settlement = False
                else:
                    # An interrupt must end the route, never a retry that catches the earlier error.
                    error.add_note(f'Interrupted while handling: {failure!r}')
                    failure = error
        if failure is not None and failure is not active_error:
            raise failure


def empty_delta_header(repo: Path, source: CheckoutSource, basis: DeltaBundle, prefix: str) -> str:
    from scripts.production.gate import checked_git

    tips = source.bundle_tips()
    if checked_git(repo, 'rev-list', '--max-count=1', *tips.values(), f'^{basis.base}'):
        return ''
    object_format = 'sha256' if len(source.commit) == 64 else 'sha1'
    # Git refuses to create a bundle when the prerequisite already has all the tips.
    refs = ''.join(f'{commit} {prefix}/{name}\n' for name, commit in tips.items())
    return f'# v3 git bundle\n@object-format={object_format}\n-{basis.base} guest main\n{refs}\n'


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
        local refs=("--glob=$prefix/*")
        if [ -n "$base" ]; then
            refs+=("^$base")
        fi
        git bundle create - "${refs[@]}"
    fi
}
send() {
    if [ -n "$count" ]; then
        IFS= read -r guest_connect_timeout || exit 1
        printf '%s\\n' "$guest_connect_timeout" || exit 1
        # Keep this sender as one process until the guest accepts the command.
        IFS= read -r upload || exit 1
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


def _guest_queue_query(request: ExecutionReport | PlacementStatusRequest, *, deadline: float, timeout: float) -> Reply:
    """Send one request within the queue query deadline."""
    from scripts.build_queue import admission_protocol as wire

    def remaining() -> float:
        left = deadline - monotonic()
        if left <= 0:
            raise TimeoutError
        return left

    try:
        address = str(wire.admission_socket_path())
        outgoing = wire.encode_request(request).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(remaining())
            connection.connect(address)
            connection.settimeout(remaining())
            connection.sendall(outgoing)
            pending = bytearray()
            while True:
                connection.settimeout(remaining())
                data = connection.recv(65536)
                if not data:
                    raise wire.AdmissionUnavailable('the queue closed the connection')
                if (end := data.find(b'\n')) >= 0:
                    pending.extend(data[:end])
                    response = wire.parse_reply(bytes(pending), request)
                    break
                pending.extend(data)
    except TimeoutError as error:
        raise wire.AdmissionUnavailable(f'no answer in {timeout:g} s') from error
    if isinstance(response, wire.Refused):
        raise wire.AdmissionUnavailable(response.reason)
    return response


def send_guest_execution(request: ExecutionReport, *, deadline: float, timeout: float) -> DispatchStatus:
    """Send one execution report within the queue query deadline."""
    from scripts.build_queue import admission_protocol as wire

    response = _guest_queue_query(request, deadline=deadline, timeout=timeout)
    if not isinstance(response, wire.DispatchStatus):
        raise wire.AdmissionUnavailable('invalid execution report answer')
    return response


def guest_permission_consumed(assignment: PlacementAssignment, claim: ProviderClaimReference) -> bool:
    """Check whether the queue saved consumed permission for this launch claim."""
    from scripts.build_queue import admission_client, admission_protocol as wire, admission_wire, placement

    query = admission_wire.PlacementStatusRequest(assignment.episode, assignment.attempt, include_permission=True,
                                                  code_version=admission_client.LOADED_ADMISSION_CODE_VERSION)
    try:
        with conf.diagnostics(report=False):
            timeout = conf.read('build_admission').number('query_timeout_seconds')
        reply = _guest_queue_query(query, deadline=monotonic() + timeout, timeout=timeout)
    except (wire.AdmissionUnavailable, OSError, ValueError):
        return False
    return (isinstance(reply, admission_wire.PlacementStatusReply)
            and isinstance(saved := reply.status, admission_wire.KnownPlacementStatus)
            and saved.assignment == assignment and saved.claim == claim
            and isinstance(saved.state, placement.CurrentAssignment)
            and isinstance(saved.permission, placement.PermissionConsumed))


def report_guest_start(armed: ArmedGuestAttempt, dispatch: GuestGateDispatch, *,
                       deadline: float, timeout: float) -> None:
    from scripts.build_queue import admission_client, admission_protocol as wire, execution_identity, placement

    assignment = dispatch.assignment
    execution = execution_identity.CloudExecution(TARGET, armed.boot_id, armed.machine_id, assignment.attempt)
    request = wire.ExecutionReport(admission_client.request_id(), assignment, dispatch.claim,
                                   execution_identity.ObservedExecution(execution),
                                   code_version=admission_client.LOADED_ADMISSION_CODE_VERSION)
    response = send_guest_execution(request, deadline=deadline, timeout=timeout)
    if (response.assignment != assignment or not isinstance(response.state, placement.CurrentAssignment)
            or not isinstance(response.permission, placement.PermissionConsumed) or response.reason):
        raise wire.AdmissionUnavailable(response.reason or 'the queue did not confirm the guest attempt')


def _append_guest_end(dispatch: GuestGateDispatch, evidence: GuestEndProven | GuestNotStartedProven | ArmedGuestAttempt,
                      detail: str) -> None:
    from scripts.build_queue.execution_identity import attempt_key

    assignment = dispatch.assignment
    _write_guest_end(attempt_key(assignment.episode, assignment.attempt), dispatch.claim.claim_id, evidence, detail)


def _write_guest_end(key: str, claim: str,
                     evidence: GuestEndProven | GuestNotStartedProven | ArmedGuestAttempt,
                     detail: str) -> None:
    row: dict[str, object] = {'attempt': key, 'claim': claim, 'detail': detail, 'written_at': time()}
    if isinstance(evidence, ArmedGuestAttempt):
        row['result'] = 'unproven'
        row['armed'] = evidence.to_record()
    else:
        row['result'] = 'proven' if isinstance(evidence, GuestEndProven) else 'not started'
    _append_guest_end_row(row)


def _append_guest_end_row(row: dict[str, object]) -> None:
    path = target_config.mac_linux_end_proof_journal()
    directory = path.parent
    state_root = directory.parent
    directory.mkdir(exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a+b') as journal:
        fcntl.flock(journal, fcntl.LOCK_EX)
        if os.fstat(fd).st_size:
            _ = journal.seek(-1, os.SEEK_END)
            if journal.read(1) != b'\n':
                # Keep the old bytes and end the damaged line before the new row.
                _ = journal.write(b'\n')
        _ = journal.write((json.dumps(row) + '\n').encode())
        journal.flush()
        os.fsync(fd)
        # A previous writer can leave the journal directory visible before it reaches disk.
        atomic_file.sync_folder(directory)
        atomic_file.sync_folder(state_root)


@dataclass(frozen=True)
class GuestEndRecovery:
    """The exact Mac claim and guest run that still need an end proof."""

    key: str
    claim: str
    armed: ArmedGuestAttempt


@dataclass(frozen=True)
class GuestLaunchHolder:
    """The gate process holds the guest attempt."""

    pid: int
    start: str

    def may_be_live(self) -> bool:
        current = process_start.process_start(self.pid)
        return (isinstance(current, process_start.Started) and current.start == self.start
                or isinstance(current, process_start.Unreadable))


@dataclass(frozen=True)
class GuestLaunchRecovery:
    """The journal identifies the guest and the gate that can arm it."""

    key: str
    claim: str
    ssh_hops: tuple[str, str]
    guest_user: str
    checkout: GuestCheckoutIdentity
    holders: tuple[GuestLaunchHolder, ...] = ()


@dataclass(frozen=True)
class GuestLaunchUnrecorded:
    """Recovery has no journal entry for the attempt."""


def _placed_guest_attempt(key: str) -> str:
    return PLACED_GUEST_ATTEMPT_PREFIX + hashlib.sha256(key.encode()).hexdigest()


def _record_guest_launch(assignment: PlacementAssignment, claim: claim_state.ClaimIdentity,
                         settings: GuestSettings) -> GuestLaunchRecovery:
    from scripts.build_queue.execution_identity import attempt_key

    pid = os.getpid()
    start = process_start.process_start(pid)
    if not isinstance(start, process_start.Started):
        raise GuestTookNoRun('the gate process has no start time to hold the launch permission')
    identity = settings.checkout_identity
    assert isinstance(identity, GuestCheckoutIdentity)
    key = attempt_key(assignment.episode, assignment.attempt)
    holder = GuestLaunchHolder(pid, start.start)
    _append_guest_end_row({'attempt': key, 'claim': claim.claim_id,
                           'result': 'awaiting arm', 'detail': 'The gate prepared to request launch permission',
                           'written_at': time(), 'mac_host': settings.ssh_hops[0], 'guest_host': settings.ssh_hops[1],
                           'guest_user': settings.guest_user,
                           'guest_checkout': {'machine_id': identity.machine_id, 'path': identity.checkout},
                           'holder': {'pid': holder.pid, 'start': holder.start}})
    return GuestLaunchRecovery(key, claim.claim_id, settings.ssh_hops, settings.guest_user, identity, (holder,))


def _release_guest_launch(launch: GuestLaunchRecovery) -> None:
    holder, = launch.holders
    _append_guest_end_row({'attempt': launch.key, 'claim': launch.claim, 'result': 'arm abandoned',
                           'detail': 'The gate gave up the attempt', 'written_at': time(),
                           'holder': {'pid': holder.pid, 'start': holder.start}})


def _read_guest_end(episode: str, attempt: int) -> Settled | Pending | GuestEndRecovery | GuestLaunchRecovery | GuestLaunchUnrecorded:
    from scripts.build_queue.execution_identity import attempt_key
    from scripts.build_queue.placement import Pending, Settled

    key = attempt_key(episode, f'{attempt:032x}')
    result: GuestEndRecovery | GuestLaunchUnrecorded = GuestLaunchUnrecorded()
    launch: GuestLaunchRecovery | None = None
    holders: dict[tuple[str, GuestLaunchHolder], None] = {}
    conflicting_address = False
    recorded = False
    proven = False
    try:
        with target_config.mac_linux_end_proof_journal().open('rb') as journal:
            for line in journal:
                if not line.endswith(b'\n'):
                    continue
                try:
                    row = ledger.mapping(cast(object, json.loads(line)))
                    when = row.get('written_at')
                    if (row.get('attempt') != key or not isinstance(row.get('detail'), str)
                            or not isinstance(when, (int, float))
                            or isinstance(when, bool) or not math.isfinite(when)):
                        continue
                    kind = row.get('result')
                    if kind in ('proven', 'not started', 'unproven', 'awaiting arm', 'arm abandoned'):
                        recorded = True
                    claim = ledger.text(row, 'claim')
                    if kind in ('proven', 'not started'):
                        proven = True
                    if kind == 'unproven' and 'armed' in row:
                        saved = ledger.mapping(row['armed'])
                        armed = ArmedGuestAttempt(ledger.text(saved, 'attempt'), ledger.text(saved, 'machine_id'),
                            ledger.text(saved, 'boot_id'), ledger.instant(saved, 'deadline_ns'),
                            ledger.text(saved, 'guest_user'), ledger.text(saved, 'checkout'))
                        result = GuestEndRecovery(key, claim, armed)
                        # The gate gives up the attempt when it saves the armed identity for recovery.
                        holders = {entry: None for entry in holders if entry[0] != claim}
                    if kind in ('awaiting arm', 'arm abandoned') and 'holder' in row:
                        saved_holder = ledger.mapping(row['holder'])
                        holder = GuestLaunchHolder(ledger.instant(saved_holder, 'pid'), ledger.text(saved_holder, 'start'))
                        if kind == 'awaiting arm':
                            holders[(claim, holder)] = None
                        else:
                            _ = holders.pop((claim, holder), None)
                    if kind == 'awaiting arm':
                        saved_guest = ledger.mapping(row.get('guest_checkout'))
                        identity = GuestCheckoutIdentity(
                            ledger.text(saved_guest, 'machine_id'), ledger.text(saved_guest, 'path'))
                        recovery = GuestLaunchRecovery(key, claim,
                            (ledger.text(row, 'mac_host'), ledger.text(row, 'guest_host')),
                            ledger.text(row, 'guest_user'), identity)
                        if launch is not None and (launch.ssh_hops, launch.guest_user, launch.checkout) != (
                                recovery.ssh_hops, recovery.guest_user, recovery.checkout):
                            conflicting_address = True
                        launch = recovery
                except (ValueError, OverflowError, RecursionError):
                    continue
    except FileNotFoundError:
        pass
    except OSError as error:
        return Pending(f'The Mac Linux end proof journal is unavailable: {error}')
    if conflicting_address:
        return Pending('The Mac Linux guest address changed for this attempt.')
    if proven:
        if any(holder.may_be_live() for _, holder in holders):
            return Pending('The Mac Linux launch holder can still report the result.')
        return Settled()
    if isinstance(result, GuestEndRecovery) or not recorded:
        return result
    if launch is None:
        return Pending('The Mac Linux recovery record is incomplete.')
    return replace(launch, holders=tuple(holder for _, holder in holders))


def settlement(episode: str, attempt: int, *, legacy_holder_ended: bool = False) -> Settled | Pending:
    """Read the proof or use the queue's holder check for a legacy attempt."""
    from scripts.build_queue.placement import Pending, Settled

    result = _read_guest_end(episode, attempt)
    if isinstance(result, GuestLaunchUnrecorded):
        return Settled() if legacy_holder_ended else Pending('The legacy Mac Linux launch holder can still be live.')
    if isinstance(result, (GuestEndRecovery, GuestLaunchRecovery)):
        return Pending('The Mac Linux guest end remains unproven')
    return result


def _warn_guest_reconcile(unit: str, detail: str) -> None:
    """Record one warning per attempt across service starts."""
    try:
        folder = target_config.mac_linux_end_proof_journal().parent / 'end-proof-warnings'
        folder.mkdir(parents=True, exist_ok=True)
        os.close(os.open(folder / unit, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        print('Mac Linux guest reconcile failed: ' + ' '.join(detail.split()), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _guest_reconcile_unit(episode: str, attempt: int) -> str:
    from scripts.build_queue.execution_identity import attempt_key

    key = attempt_key(episode, f'{attempt:032x}')
    return 'mac-linux-reconcile-' + hashlib.sha256(key.encode()).hexdigest()


def reconcile_unproven(episode: str, attempt: int) -> None:
    """Start recovery of an unsettled guest attempt without waiting for SSH."""
    unit = _guest_reconcile_unit(episode, attempt)
    try:
        if not _guest_recovery_ready(_read_guest_end(episode, attempt)):
            return
        from scripts.aws_target import cloud_gate

        overrides = [f'--setenv={key}={os.environ[key]}'
                     for key in ('CONF_DIR', 'BUILD_TARGETS_STATE_DIR', 'PATH') if key in os.environ]
        command = ['systemd-run', '--user', '--no-block', '--collect', '--unit=' + unit,
                   '--property=UMask=0077', '--working-directory=' + str(Path(__file__).resolve().parents[2]),
                   *overrides, sys.executable, '-m', 'scripts.build_targets.mac_linux',
                   '--reconcile-end', episode, str(attempt)]
        if _guest_recovery_ready(_read_guest_end(episode, attempt)):
            cloud_gate.start_reconcile_service(command, SSH_CONNECT_TIMEOUT_SECONDS, existing_unit=unit)
    except Exception as error:
        _warn_guest_reconcile(unit, str(error) or type(error).__name__)


def _guest_recovery_ready(recovery: Settled | Pending | GuestEndRecovery | GuestLaunchRecovery | GuestLaunchUnrecorded) -> bool:
    if isinstance(recovery, GuestLaunchRecovery):
        return not any(holder.may_be_live() for holder in recovery.holders)
    return isinstance(recovery, GuestEndRecovery)


def reconcile_guest_end(episode: str, attempt: int) -> None:
    """Use the saved guest identity to prove the attempt ended or never started."""
    unit = _guest_reconcile_unit(episode, attempt)
    try:
        recovery = _read_guest_end(episode, attempt)
        if isinstance(recovery, GuestEndRecovery):
            proof = prove_kept_guest_end(recovery.armed)
            detail = 'The guest confirmed the run ended'
        elif isinstance(recovery, GuestLaunchRecovery):
            if not _guest_recovery_ready(recovery):
                return
            settings = read_settings(conf.read('build_targets'))
            if (settings.ssh_hops, settings.guest_user) != (recovery.ssh_hops, recovery.guest_user):
                _warn_guest_reconcile(unit, 'The Mac Linux guest address changed for this attempt.')
                return
            settings = replace(settings, checkout_identity=recovery.checkout)
            proof = recover_guest_launch(settings, _placed_guest_attempt(recovery.key))
            detail = ('The guest refused the attempt before it armed' if isinstance(proof, GuestNotStartedProven)
                      else 'The guest ended the armed attempt')
        else:
            return
        if isinstance(proof, GuestEndPending):
            _warn_guest_reconcile(unit, proof.detail)
            return
        _write_guest_end(recovery.key, recovery.claim, proof, detail)
    except Exception as error:
        _warn_guest_reconcile(unit, str(error) or type(error).__name__)


def settle_guest_dispatch(dispatch: GuestGateDispatch, result: AttemptCompleted | NoTerminalReport) -> None:
    """Save the end proof before reporting the result."""
    from scripts.build_queue import admission_client, admission_protocol as wire, execution_identity, placement
    from scripts.build_queue import admission_wire as status_wire

    try:
        _append_guest_end(dispatch, GuestEndProven(), 'The guest confirmed the run ended')
    except (OSError, ValueError) as error:
        with suppress(OSError, ValueError):
            print(f'Mac Linux guest settlement report failed: {error}', file=sys.stderr, flush=True)
        return

    started = dispatch.started
    if started is None or isinstance(result, placement.NoTerminalReport):
        return
    assignment = dispatch.assignment
    execution = execution_identity.CloudExecution(TARGET, started.boot_id, started.machine_id, assignment.attempt)

    def send(deadline: float, timeout: float) -> None:
        request = wire.ExecutionReport(admission_client.request_id(), assignment, dispatch.claim,
            execution_identity.ObservedExecution(execution), result,
            placement.DispatchPathSettled(), code_version=admission_client.LOADED_ADMISSION_CODE_VERSION)
        reply = send_guest_execution(request, deadline=deadline, timeout=timeout)
        if (reply.assignment != assignment or reply.reason
                or not isinstance(reply.permission, placement.PermissionConsumed)
                or not (isinstance(reply.state, placement.CurrentAssignment)
                        or reply.state == placement.ClosedAssignment('completed'))):
            raise wire.AdmissionUnavailable(reply.reason or 'the queue did not accept the guest end report')
        if isinstance(reply.state, placement.CurrentAssignment):
            query = status_wire.PlacementStatusRequest(assignment.episode, assignment.attempt, include_permission=True,
                                                      code_version=admission_client.LOADED_ADMISSION_CODE_VERSION)
            response = _guest_queue_query(query, deadline=deadline, timeout=timeout)
            if (not isinstance(response, status_wire.PlacementStatusReply)
                    or not isinstance(saved := response.status, status_wire.KnownPlacementStatus)
                    or saved.assignment != assignment or saved.claim != dispatch.claim
                    or not isinstance(saved.permission, placement.PermissionConsumed)
                    or not isinstance(saved.report, placement.TerminalAttemptReport) or saved.report.outcome != result):
                raise wire.AdmissionUnavailable('the queue did not save the guest completion')

    def warn(reason: str) -> None:
        with suppress(OSError, ValueError):
            print(f'Mac Linux guest completion report failed: {reason}', file=sys.stderr, flush=True)

    with ExitStack() as reports:
        _start_guest_queue_report(send, math.inf, reports, warn)


def save_guest_no_start(dispatch: GuestGateDispatch) -> None:
    """Save proof that the guest received no arm command."""
    try:
        _append_guest_end(dispatch, GuestNotStartedProven(), 'The guest received no arm command')
    except (OSError, ValueError) as error:
        print(f'Mac Linux guest settlement report failed: {error}', file=sys.stderr, flush=True)


def warn_guest_report(reason: str) -> None:
    print(f'Mac Linux guest queue report failed: {reason}', file=sys.stderr, flush=True)


def start_guest_report(armed: ArmedGuestAttempt, dispatch: GuestGateDispatch, transfer_deadline: float,
                       reports: ExitStack) -> None:
    def send(deadline: float, timeout: float) -> None:
        report_guest_start(armed, dispatch, deadline=deadline, timeout=timeout)

    _start_guest_queue_report(send, transfer_deadline, reports, warn_guest_report)


def _start_guest_queue_report(send: Callable[[float, float], None], deadline_limit: float,
                              reports: ExitStack, warn: Callable[[str], None]) -> None:
    """Read the timeout before starting the report worker. Bound both waits."""
    started = monotonic()
    preparation_deadline = min(deadline_limit, started + SSH_CONNECT_TIMEOUT_SECONDS)
    ready: Future[Callable[[], None]] = Future()

    def prepare() -> None:
        try:
            with conf.diagnostics(report=False):
                timeout = conf.read('build_admission').number('query_timeout_seconds')
            deadline = min(deadline_limit, started + timeout)
            dispatch_deadline = min(preparation_deadline, deadline)
            if monotonic() >= dispatch_deadline:
                raise TimeoutError('the queue report deadline expired during configuration')
            result: Future[None] = Future()

            def report() -> None:
                try:
                    if monotonic() >= dispatch_deadline:
                        raise TimeoutError(f'no answer in {timeout:g} s')
                    send(deadline, timeout)
                    if monotonic() >= deadline:
                        raise TimeoutError(f'no answer in {timeout:g} s')
                except Exception as error:
                    result.set_exception(error)
                else:
                    result.set_result(None)

            worker = Thread(target=report, daemon=True)
            worker.start()
        except Exception as error:
            ready.set_exception(error)
            return

        def collect_report() -> None:
            worker.join(timeout=max(0.0, deadline - monotonic()))
            if not result.done():
                raise TimeoutError(f'no answer in {timeout:g} s')
            result.result()

        ready.set_result(collect_report)

    preparation = Thread(target=prepare, daemon=True)
    try:
        preparation.start()
    except RuntimeError as error:
        warn(str(error))
        return

    def collect() -> None:
        preparation.join(timeout=max(0.0, preparation_deadline - monotonic()))
        if not ready.done():
            warn('the queue report configuration read timed out')
            return
        try:
            ready.result()()
        except Exception as error:
            warn(str(error) or type(error).__name__)

    _ = reports.callback(collect)


def ssh_connect_timeout(remaining: float) -> int:
    # OpenSSH uses whole seconds. Use one second for the outer hop when the local deadline is shorter.
    return max(1, min(SSH_CONNECT_TIMEOUT_SECONDS, int(remaining)))


def communicate_guest_bundle(stream: subprocess.Popen[bytes], bootstrap: bytes, timeout: int,
                             armed: ArmedGuestAttempt, dispatch: GuestGateDispatch,
                             reports: ExitStack, *, connect_deadline: float = math.inf) -> tuple[bytes, bytes]:
    """Transfer the bootstrap and read the reply, progress records, and diagnostics."""
    from scripts.aws_target import cloud_gate
    from scripts.build_queue import progress_profile

    assert stream.stdin is not None and stream.stdout is not None and stream.stderr is not None
    stream_input = stream.stdin
    payload = bootstrap
    connected = connect_deadline == math.inf
    budget_sent = connected
    deadline = monotonic() + timeout if connected else connect_deadline
    mac_ready_line = bytearray(INNER_CONNECT_READY.encode())
    accepted_line = f'{GUEST_ACCEPTED} {armed.attempt}'.encode()
    start_line = run_start_line(armed.attempt).encode()
    progress_prefix = GUEST_PROGRESS.encode()
    sidecar = os.environ.get('BUILDLOG_PLACEMENT_SIDECAR', '')
    stdout, stderr = bytearray(), bytearray()
    reply_line = bytearray()
    progress = cloud_gate.ModuleProgress()
    last_counter: progress_profile.ProgressCounter | progress_profile.NoCounter = progress_profile.NoCounter()
    started = False

    def observe_guest(line: bytes) -> None:
        nonlocal connected, deadline, payload, started
        line = line.rstrip(b'\r')
        if not connected and budget_sent and line == accepted_line:
            now = monotonic()
            if now < deadline:
                connected = True
                deadline = now + timeout
                payload = b'\n' + bootstrap
                if not stream_input.closed:
                    _ = ready.register(input_fd, EVENT_WRITE)
        if not connected or started or line != start_line:
            return
        started = True
        dispatch.started = armed
        start_guest_report(armed, dispatch, deadline, reports)

    def read_reply(data: bytes) -> None:
        nonlocal last_counter
        *lines, tail = data.split(b'\n')
        for line in lines:
            reply_line.extend(line)
            if not budget_sent and reply_line == mac_ready_line:
                if input_fd not in ready.get_map():
                    _ = ready.register(input_fd, EVENT_WRITE)
            elif not reply_line.startswith(progress_prefix):
                stdout.extend(reply_line)
                stdout.append(10)
            elif sidecar:
                with suppress(OSError, ValueError, RecursionError):
                    record = cast(object, json.loads(reply_line[len(progress_prefix):]))
                    if isinstance(record, str):
                        progress.observe(record)
                        counter = progress.counter()
                        if isinstance(counter, progress_profile.ProgressCounter) and counter != last_counter:
                            progress_profile.publish_counter(Path(sidecar), counter)
                            last_counter = counter
            reply_line.clear()
        reply_line.extend(tail)

    input_fd, output_fd, error_fd = stream_input.fileno(), stream.stdout.fileno(), stream.stderr.fileno()
    sent = parsed = 0
    try:
        with DefaultSelector() as ready:
            if connected:
                if bootstrap:
                    _ = ready.register(input_fd, EVENT_WRITE)
                else:
                    stream_input.close()
            _ = ready.register(output_fd, EVENT_READ)
            _ = ready.register(error_fd, EVENT_READ)
            while ready.get_map():
                left = deadline - monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(stream.args, timeout)
                for key, _events in ready.select(left):
                    if key.fd == input_fd:
                        if not budget_sent:
                            remaining = deadline - monotonic()
                            if remaining <= 0:
                                raise subprocess.TimeoutExpired(stream.args, timeout)
                            if remaining < 1:
                                raise GuestTookNoRun('Less than one second remains for the guest SSH connection.')
                            try:
                                _ = os.write(input_fd, f'{ssh_connect_timeout(remaining)}\n'.encode())
                            except BrokenPipeError:
                                stream_input.close()
                            budget_sent = True
                            _ = ready.unregister(input_fd)
                            continue
                        try:
                            sent += os.write(input_fd, payload[sent:sent + PIPE_BUF])
                        except BrokenPipeError:
                            sent = len(payload)
                        if sent == len(payload):
                            _ = ready.unregister(input_fd)
                            stream_input.close()
                        continue
                    data = os.read(key.fd, 65536)
                    if not data:
                        _ = ready.unregister(key.fd)
                        if key.fd == error_fd and parsed < len(stderr):
                            observe_guest(bytes(stderr[parsed:]))
                    elif key.fd == output_fd:
                        read_reply(data)
                    else:
                        offset = len(stderr)
                        stderr.extend(data)
                        while not (connected and started) and (line_end := stderr.find(b'\n', offset)) >= 0:
                            observe_guest(bytes(stderr[parsed:line_end]))
                            parsed = offset = line_end + 1
        _ = stream.wait(timeout=max(0, deadline - monotonic()))
    except subprocess.TimeoutExpired as error:
        error.stdout = bytes(stdout + reply_line)
        error.stderr = bytes(stderr)
        if not connected:
            detail = stream_diagnostic(bytes(stderr))
            raise GuestTookNoRun(f'The guest SSH connect deadline expired before command acceptance: {detail}') from error
        raise
    return bytes(stdout + reply_line), bytes(stderr)


def stream_guest_bundle(repo: Path, command: list[str], bootstrap: bytes, timeout: int,
                        link: int, armed: ArmedGuestAttempt, dispatch: GuestGateDispatch, *,
                        connect_deadline: float = math.inf) -> subprocess.CompletedProcess[bytes]:
    if monotonic() >= connect_deadline:
        raise GuestConnectAttemptsFailed('the guest connect deadline expired before SSH')
    with (ExitStack() as reports,
          subprocess.Popen(command, cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, start_new_session=True, pass_fds=(link,),
                           env=guest_ssh_environment()) as stream):
        try:
            output, errors = communicate_guest_bundle(stream, bootstrap, timeout, armed, dispatch, reports,
                                                       connect_deadline=connect_deadline)
        except BaseException as error:
            stop_ssh_process(stream)
            if isinstance(error, subprocess.TimeoutExpired):
                stderr = error.stderr if isinstance(error.stderr, bytes) else b''
                started = run_start_line(armed.attempt).encode() in stderr.split(b'\n')
                failure = GuestResultUnavailable if started else GuestTookNoRun
                raise failure(f'SSH or guest run exceeded {timeout} seconds: {stream_diagnostic(stderr)}') from error
            raise
    return subprocess.CompletedProcess(command, stream.returncode, output, errors)


def bundle_send(repo: Path, source: CheckoutSource, bundle: FullBundle | DeltaBundle, prefix: str) -> BundleSend:
    if isinstance(bundle, DeltaBundle):
        return BundleSend(bundle.base, empty_delta_header(repo, source, bundle, prefix), BundleForm.DELTA, BundleReason.DELTA)
    return BundleSend('', '', BundleForm.FIRST, bundle.reason)


def stream_arguments(settings: GuestSettings, shell: str, prefix: str, send: BundleSend, *,
                     count: bool, link: int, connect_timeout: int = SSH_CONNECT_TIMEOUT_SECONDS) -> list[str]:
    """Send the bootstrap and bundle, then hold the link."""
    command = settings.command(shell, diagnose_connect=count, budget_inner_connect=count,
                               connect_timeout=connect_timeout)
    return ['bash', '-o', 'pipefail', '-c', STREAM_SOURCE, TARGET, prefix, send.base, send.header,
            'count' if count else '', str(link), *command]


def run_remote(repo: Path, settings: GuestSettings, source: CandidateSource, request: GuestRequest,
               prefix: str, bundle: FullBundle | DeltaBundle, dispatch: GuestGateDispatch) -> GuestReply:
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
                        prior_arm, prior_settlement = dispatch.arm_requested, dispatch.settled
                        try:
                            dispatch.arm_requested = True
                            dispatch.settled = False
                            armed = arm_guest(settings, request['attempt'])
                        except (GuestArmNotSent, GuestConnectAttemptsFailed) as error:
                            dispatch.arm_requested, dispatch.settled = prior_arm, prior_settlement
                            raise GuestTookNoRun(f'the gate could not send the arm request: {error}') from error
                        except (GuestUnavailable, OSError, subprocess.SubprocessError) as error:
                            raise GuestTookNoRun(f'the guest did not arm the run: {error}') from error
                        armed_for_stream = armed
                        with guest_link() as link:
                            acceptance = shlex.quote(f'{GUEST_ACCEPTED} {armed.attempt}')
                            shell = f"printf '%s\\n' {acceptance} >&2\n"
                            shell += run_shell(settings, source, len(bootstrap), armed)
                            connect_deadline = monotonic() + settings.unreachable_wait_seconds

                            def send_stream(remaining: float) -> subprocess.CompletedProcess[bytes]:
                                arguments = stream_arguments(
                                    settings, shell, prefix, send, count=True, link=link,
                                    connect_timeout=ssh_connect_timeout(remaining))
                                return stream_guest_bundle(
                                    repo, arguments, bootstrap, settings.transfer_timeout_seconds, link,
                                    armed_for_stream, dispatch, connect_deadline=connect_deadline)

                            stream = retry_guest_connect(send_stream, connect_deadline, bundle_stream=True)
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
        retry_before_settlement = (dispatch.started is None
            and (stream is None or run_start_line(request['attempt']).encode() not in stream.stderr.splitlines())
            and (first_error is None or isinstance(first_error, GuestTookNoRun | GuestNotLocked)))
        error = end.unreachable(retry_before_settlement=retry_before_settlement)
        if stream is not None and stream.returncode:
            error.add_note(stream_report(stream))
        if first_error is not None and isinstance(first_error.__cause__, subprocess.TimeoutExpired):
            error.add_note(str(first_error))
        raise error

    dispatch.settled = True
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
    lines = diagnostics.splitlines()
    tests_started = run_start_line(attempt) in lines
    if (stream.returncode == GUEST_NOT_LOCKED_STATUS and not tests_started
            and f'{GUEST_ACCEPTED} {attempt}' in lines and run_locked_line(attempt) not in lines):
        raise GuestNotLocked(f'{stream_report(stream)}. {cleanup_detail}')
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
    return f'SSH or source stream exited {stream.returncode}: {stream_diagnostic(stream.stderr)}'


def stream_diagnostic(stderr: bytes) -> str:
    return ' '.join(stderr.decode(errors='replace').split())[-1200:] or 'no diagnostic'


class GuestResultUnavailable(GuestUnavailable):
    """The guest started tests and supplied no usable result."""


def guest_failure(tests_started: bool, reason: str) -> GuestUnavailable:
    """Distinguish a missing verdict from a failure before tests started."""
    return (GuestResultUnavailable if tests_started else GuestTookNoRun)(reason)


def guest_probe_failure(probe: str, error: Exception) -> GuestTookNoRun:
    from scripts.mac_test import offload

    refused = GuestTookNoRun(f'the guest {probe} probe failed: {error}')
    try:
        delay = conf.read('mac_test').number('unreachable_backoff_s')
        offload.write_backoff(time() + delay, TARGET, strict=True)
    except (conf.Unusable, OSError) as backoff_error:
        refused.add_note(f'Cannot record the {TARGET} backoff: {backoff_error}')
    return refused


def guest_bundle(repo: Path, settings: GuestSettings) -> FullBundle | DeltaBundle:
    """Probe the guest main commit and omit the history already on the guest."""
    try:
        guest = read_guest_main(settings)
    except (GuestUnavailable, OSError, subprocess.SubprocessError) as error:
        raise guest_probe_failure('main', error) from error
    if isinstance(guest, EmptyGuestMain):
        return FullBundle(BundleReason.FIRST_RUN)
    if subprocess.run(['git', 'cat-file', '-e', f'{guest.commit}^{{commit}}'], cwd=repo,
                      capture_output=True, timeout=TRANSFER_ALLOWANCE_SECONDS, check=False).returncode:
        return FullBundle(BundleReason.SOURCE_LACKS_BASE)
    return DeltaBundle(guest.commit)


def transfer_source(repo: Path, settings: GuestSettings, source: CandidateSource, request: GuestRequest,
                    dispatch: GuestGateDispatch) -> GuestReply:
    bundle = guest_bundle(repo, settings)
    with bundle_refs(repo, source, request['attempt']) as prefix:
        return run_remote(repo, settings, source, request, prefix, bundle, dispatch)


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
        """Start one run; the bootstrap file holds the request while the stream reads it.

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
                                      origin_main=source.origin_main, runner_commit=source.runner_commit, unit=unit,
                                      work=work, argv=list(argv),
                                      checked=sorted(files))
        data = runner_bootstrap(request)
        _ = bootstrap.write_bytes(data)
        bundle = guest_bundle(repo, self.settings)
        prefix = self.refs.enter_context(bundle_refs(repo, source, request['attempt']))
        send = bundle_send(repo, source, bundle, prefix)
        self.armed = arm_guest(self.settings, request['attempt'])
        link = self.refs.enter_context(guest_link())
        shell = run_shell(self.settings, source, len(data), self.armed)
        arguments = stream_arguments(self.settings, shell, prefix, send, count=False, link=link)
        with bootstrap.open('rb') as stdin:
            self.process = subprocess.Popen(arguments, cwd=repo, stdin=stdin, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(link,),
                                            env=guest_ssh_environment())
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


def route(repo: Path, *, mode: SuiteMode, full: bool, module_time_limit_s: float | None,
          selected: AllModules | TreeSelection, gate_job: StepJob | GateJobNotRecorded,
          assignment: PlacementAssignment, type_dirs: Sequence[str] = ()) -> GateRun | GuestNotStarted | GuestGateBlocked | str:
    """Run a gate on the assigned guest.

    The assignment is the one the gate checked at its start. The route consumes its launch permission.
    A None module limit uses the configured full-run limit.
    """
    from scripts.aws_target import cloud_gate
    from scripts.build_queue import mac_run, placement
    from scripts.build_queue.admission_client import step_identity
    from scripts.build_queue.execution_identity import attempt_key
    from scripts.production import gate

    dispatch: GuestGateDispatch | None = None
    launch: GuestLaunchRecovery | None = None
    completion: placement.AttemptCompleted | placement.NoTerminalReport = placement.NoTerminalReport()

    def release_launch() -> None:
        nonlocal launch
        assert launch is not None
        saved, launch = launch, None
        _release_guest_launch(saved)

    def refused(reason: str, *, uncertain_claim: ProviderClaimReference | None = None,
                lock_refused: bool = False) -> GuestNotStarted | GuestGateBlocked:
        nonlocal dispatch
        if dispatch is None:
            if uncertain_claim is None:
                return GuestNotStarted(reason)
            if guest_permission_consumed(assignment, uncertain_claim):
                dispatch = GuestGateDispatch(assignment, uncertain_claim)
        if dispatch is not None:
            if dispatch.arm_requested:
                if dispatch.started is not None:
                    return GuestGateBlocked(reason)
                if dispatch.settled and not lock_refused:
                    reason += '. The guest did not confirm whether the run started.'
                    return GuestNotStarted(reason, settlement_required=True, retry_before_settlement=True)
            else:
                save_guest_no_start(dispatch)
        try:
            release_launch()
        except OSError as error:
            return GuestGateBlocked(f'{reason}. Cannot release the Mac Linux launch holder: {error}')
        if dispatch is None:
            return GuestNotStarted(reason, settlement_required=True)
        return GuestNotStarted(reason, settlement_required=True, retry_before_settlement=True)

    try:
        # natedev selects the modules of a targeted run once; the guest runs that list and selects none itself.
        # run_gate_suites always sends a list for a targeted run without --full, so this guard protects only a future caller.
        if mode is SuiteMode.TARGETED and not full and isinstance(selected, AllModules):
            raise GuestUnavailable('the targeted run has no module list from the natedev selection')
        settings = new_run_settings()
        if not isinstance(settings, GuestSettings):
            return settings
        root = target_config.build_targets_state_root()
        if not root.is_dir():
            return GuestNotStarted(f'The Mac Linux state root {root} must be a directory. '
                                   + 'The admission service creates it at startup. '
                                   + 'To repair it, run build_targets.py setup-mac-linux on natedev.')
        run_time_limit_seconds = settings.run_time_limit_seconds
        if module_time_limit_s is None:
            module_time_limit_s = run_time_limit_seconds
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
        guest_attempt = _placed_guest_attempt(attempt_key(assignment.episode, assignment.attempt))
        request = GuestRequest(attempt=guest_attempt, commit=source.commit, tree=source.tree,
                               origin_main=source.origin_main, runner_commit=source.runner_commit,
                               mode=mode.wire, full=full,
                               selection=list(selected.paths) if isinstance(selected, TreeSelection) else 'all',
                               type_dirs=relative_type_dirs,
                               module_time_limit_s=module_time_limit_s, run_time_limit_seconds=run_time_limit_seconds,
                               jobs=settings.jobs, unit=unit)
        try:
            with mac_linux_reservation(repo, mode, gate_job) as claim:
                try:
                    identity = read_guest_checkout(settings, repo)
                except (GuestUnavailable, OSError, subprocess.SubprocessError) as error:
                    raise guest_probe_failure('checkout', error) from error
                settings = replace(settings, checkout_identity=identity)
                launch = _record_guest_launch(assignment, claim, settings)
                dispatch = GuestGateDispatch(assignment, consume_launch_permission(launch, assignment))
                reply = transfer_source(repo, settings, source, request, dispatch)
                run = completed_run(reply, request, source)
        except GuestMissingPrerequisite:
            if dispatch is None:
                raise
            request = request.copy()
            request['attempt'] = guest_attempt + FULL_BUNDLE_RETRY_SUFFIX
            with (mac_linux_reservation(repo, mode, gate_job),
                  bundle_refs(repo, source, request['attempt']) as prefix):
                reply = run_remote(repo, settings, source, request, prefix,
                                   FullBundle(BundleReason.GUEST_LACKS_PREREQUISITE), dispatch)
                run = completed_run(reply, request, source)
        status = 0 if run.passed else next((job.returncode for job in run.jobs if job.returncode), 1)
        checkout_changed = (gate.checked_git(repo, 'rev-parse', 'HEAD') != source.head
                            or cloud_gate.working_tree(repo)[0] != source.tree)
        completion = placement.AttemptCompleted((status or 1) if checkout_changed else status)
        if checkout_changed:
            return GuestGateBlocked('the checkout changed during the guest run')
        return run
    except mac_run.MacRunRefused as error:
        return refused(str(error))
    except (GuestTookNoRun, GuestNotLocked) as error:
        notes = cast(list[str], getattr(error, '__notes__', []))
        return refused(' '.join((str(error), *notes)),
                       uncertain_claim=error.claim if isinstance(error, GuestPermissionUnknown) else None,
                       lock_refused=isinstance(error, GuestNotLocked))
    except GuestEndUnproven as error:
        if dispatch is not None:
            try:
                _append_guest_end(dispatch, error.armed, str(error))
                launch = None
            except OSError as journal_error:
                error.add_note(f'Cannot save the Mac Linux guest end: {journal_error}')
                try:
                    release_launch()
                except OSError as release_error:
                    detail = f'{error}. Cannot save the Mac Linux guest end: {journal_error}.'
                    return GuestGateBlocked(f'{detail} Cannot release the Mac Linux launch holder: {release_error}')
            else:
                if error.retry_before_settlement and dispatch.started is None:
                    notes = cast(list[str], getattr(error, '__notes__', []))
                    return GuestNotStarted(' '.join((str(error), *notes)), settlement_required=True,
                                           retry_before_settlement=True)
        raise
    except GuestResultUnavailable as error:
        notes = cast(list[str], getattr(error, '__notes__', []))
        raise GateResultUnknown(f'{TARGET}: ' + ' '.join((str(error), *notes))) from error
    except route_errors() as error:
        notes = cast(list[str], getattr(error, '__notes__', []))
        detail = ' '.join((str(error), *notes))
        if dispatch is not None:
            return GuestGateBlocked(detail) if dispatch.arm_requested else refused(detail)
        return f'{TARGET}: {detail}'
    finally:
        if dispatch is not None and dispatch.settled:
            settle_guest_dispatch(dispatch, completion)
        if launch is not None and (dispatch is None or not dispatch.settled):
            try:
                release_launch()
            except OSError as error:
                print(f'Cannot save the Mac Linux guest end: {error}', file=sys.stderr, flush=True)


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
    """Return the checkout, the Cargo registry and git folders, and the Cargo lock files."""
    home = home_folder(environment)
    cargo_home = Path(environment.get('CARGO_HOME') or home / '.cargo')
    return [checkout, cargo_home / 'registry', cargo_home / 'git', *(cargo_home / name for name in CARGO_LOCKS)]


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
    # Empty wrapper values turn off the Cargo config wrapper.
    return [*words, '--remount-ro', '/', '--setenv', 'TMPDIR', str(TEMP), '--setenv', 'CARGO_NET_OFFLINE', 'true',
            '--setenv', 'RUSTC_WRAPPER', '', '--setenv', 'CARGO_BUILD_RUSTC_WRAPPER', '',
            '--unsetenv', 'SCCACHE_DIR', '--unsetenv', 'SCCACHE_CACHE_SIZE', '--unsetenv', 'SCCACHE_SERVER_UDS',
            '--chdir', str(checkout), '--']


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

    The program writes the copy and its Cargo folders. The copy holds the run home and the build output.
    The program sees the attempt temp folder as /tmp.
    The limit binds no path that holds the guest account home. START_SHELL prints the start line in the limit.
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
                return run_sandboxed_gate(tmp, gate_entry_command(request['jobs'], Path(__file__).resolve(),
                                                                  request_path, reply),
                                          copy.path, environment, reply)
            finally:
                os.close(reply)


def run_sandboxed_gate(tmp: Path, command: Sequence[str], copy: Path, environment: dict[str, str],
                       reply: int) -> int:
    """Run the gate in the sandbox with its standard output on standard error.

    The gate sends progress and the result through the reply descriptor.
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
    """Start the archived runner with the numbered copy as the working directory."""
    return [str(runner.parents[1] / 'lib/py'), '-X', f'cpu_count={jobs}',
            str(runner), str(request_path), RUN_COPY, str(reply)]


def guest_request(path: Path) -> GuestRequest | GuestCommandRequest:
    return cast(GuestRequest | GuestCommandRequest, json.loads(path.read_text(encoding='utf-8')))


def run_copy(request_path: Path, reply: int) -> None:
    """Run the candidate's gate and send progress and one JSON result on the reply descriptor."""
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
    token = copies.COMMAND_LOCKS.set(descriptors)
    # natedev reads this line: a failure before it lets natedev take the gate.
    print(run_start_line(request['attempt']), file=sys.stderr, flush=True)
    try:
        with (build_folders.inherit_build_lock(RUN_LOCK_FD),
              os.fdopen(reply, 'w', encoding='utf-8') as output):
            def send_progress(line: str) -> None:
                print(GUEST_PROGRESS + json.dumps(line), file=output, flush=True)

            # Older gate code can omit these fields.
            if hasattr(gate.WholeMachine, 'rerun_failed_modules'):
                machine = gate.WholeMachine(module_progress=send_progress, rerun_failed_modules=True)
            elif hasattr(gate.WholeMachine, 'module_progress'):
                machine = gate.WholeMachine(module_progress=send_progress)
            else:
                machine = gate.WholeMachine()
            run = gate.run_suites(repo, type_dirs=tuple(map(str, type_dirs)), full=request['full'],
                                  mode=SuiteMode(request['mode']), machine=machine,
                                  selected=ALL_MODULES if selection == 'all' else SelectedModules(tuple(selection)),
                                  module_time_limit_s=request['module_time_limit_s'],
                                  time_limit=GateTimeLimit(request['run_time_limit_seconds']))
            print(json.dumps({'attempt': request['attempt'], 'commit': commit, 'tree': tree, 'origin_main': origin_main,
                              'verdict': 'pass' if run.passed else 'fail', 'gate': gate_box.encode_run(run)}),
                  file=output)
    finally:
        copies.COMMAND_LOCKS.reset(token)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--reconcile-end']:
        reconcile_guest_end(sys.argv[2], int(sys.argv[3]))
    elif sys.argv[2:3] == [RUN_COPY]:
        run_copy(Path(sys.argv[1]), int(sys.argv[3]))
    else:
        raise SystemExit(run_guest(Path(sys.argv[1])))

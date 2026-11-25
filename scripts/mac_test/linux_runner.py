#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Run leased Linux commands and read process ownership."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from array import array
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from enum import Enum
import fcntl
import json
import math
import os
import re
import selectors
from pathlib import Path
import shlex
import shutil
import subprocess
import termios
import time
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast
import uuid

from scripts.lib import process_stat
from scripts.lib.dev_shell import (
    ShellEnvironment as ShellEnvironment,
    exports_are_valid as exports_are_valid,
    prepare_shell as prepare_shell,
    shell_inputs as shell_inputs,
    shell_key as shell_key,
)
from scripts.mac_test import floor_claims

if TYPE_CHECKING:
    from scripts.lib import build_copies, build_folders
    from scripts.lib.boot_identity import boot_identity as boot_identity
    from scripts.lib.cargo_output import CargoOutputUnavailable, read_cargo_output_roots
else:
    from scripts.lib import build_folders
    from scripts.lib.boot_identity import boot_identity as boot_identity
    from scripts.lib.cargo_output import CargoOutputUnavailable, read_cargo_output_roots


PROC_ROOT = Path('/proc')


class SlotBoot(Enum):
    CURRENT = 'current'
    PREVIOUS = 'previous'
    LEGACY = 'legacy'


def slot_boot(lease: SlotRun, current_boot: str, *, require_boot_identity: bool = False) -> SlotBoot:
    """Keep unconfirmed AWS ownership separate from a confirmed earlier boot."""
    if 'boot_id' not in lease:
        if require_boot_identity:
            raise SlotProcessesUnresolved('retained marker has no boot identity')
        return SlotBoot.LEGACY
    identity = lease['boot_id']
    if not identity:
        raise ValueError('invalid slot boot identity')
    return SlotBoot.CURRENT if identity == current_boot else SlotBoot.PREVIOUS


@dataclass(frozen=True)
class LeasedSlotExecution:
    """Execute with the maker's held slot and its Cargo environment."""

    slot: build_copies.LeasedCopy
    descriptors: tuple[int, ...]


@dataclass(frozen=True)
class LeasedCommandResult:
    status: int
    stdout: bytes
    stderr: bytes
    started_at: float
    finished_at: float
    execution_seconds: float


class LeasedCommandLost(RuntimeError):
    """A started command cannot return its complete output."""


def execution_descriptors() -> tuple[int, ...]:
    """Keep mirror locks and maker locks through every command child."""
    return tuple(dict.fromkeys((*floor_claims.inherited_mirror_descriptors(),
                               *build_folders.held_build_folder_descriptors())))


def _pending_pipe_bytes(fd: int) -> int:
    count = array('i', [0])
    _ = cast(int, fcntl.ioctl(fd, termios.FIONREAD, count))
    return count[0]


def capture_leased_command(command: list[str], execution: LeasedSlotExecution,
                           environment: Mapping[str, str], output_max_bytes: int, *, timeout_s: float,
                           extra_descriptors: tuple[int, ...] = ()) -> LeasedCommandResult:
    """Capture output through command exit, including bytes still pending at exit."""
    if output_max_bytes <= 0 or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError('The command output bound and deadline must be positive')
    started_at, started = time.time(), time.monotonic()
    deadline = started + timeout_s
    output = [bytearray(), bytearray()]
    child = subprocess.Popen(command, cwd=execution.slot.path, env=dict(environment),
            pass_fds=tuple(dict.fromkeys((*execution_descriptors(), *execution.descriptors, *extra_descriptors))),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    assert child.stdout is not None and child.stderr is not None
    pipe_fds = (child.stdout.fileno(), child.stderr.fileno())
    with ExitStack() as cleanup:
        _ = cleanup.enter_context(child.stdout)
        _ = cleanup.enter_context(child.stderr)
        exit_fd = os.pidfd_open(child.pid)
        _ = cleanup.callback(os.close, exit_fd)
        streams = cleanup.enter_context(selectors.DefaultSelector())
        _ = streams.register(exit_fd, selectors.EVENT_READ)
        for index, fd in enumerate(pipe_fds):
            os.set_blocking(fd, False)
            _ = streams.register(fd, selectors.EVENT_READ, index)

        while True:
            remaining = deadline - time.monotonic()
            events = streams.select(max(0, remaining))
            if any(key.fd == exit_fd for key, _ in events):
                break
            if remaining <= 0:
                # The systemd unit owns process cleanup.
                raise LeasedCommandLost('The Hana command exceeded its execution deadline')
            for key, _ in events:
                try:
                    content = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not content:
                    _ = streams.unregister(key.fd)
                    continue
                if sum(map(len, output)) + len(content) > output_max_bytes:
                    raise LeasedCommandLost('The command output exceeds its transport bound')
                output[cast(int, key.data)].extend(content)

        finished_at, finished = time.time(), time.monotonic()
        # A descendant can write while the runner reads either pipe.
        pending = tuple(_pending_pipe_bytes(fd) for fd in pipe_fds)
        status = child.wait()
        if sum(map(len, output)) + sum(pending) > output_max_bytes:
            raise LeasedCommandLost('The command output exceeds its transport bound')
        for fd, remaining, buffer in zip(pipe_fds, pending, output, strict=True):
            while remaining > 0:
                content = os.read(fd, min(remaining, 65536))
                if not content:
                    raise LeasedCommandLost('The command output ended before the pending bytes were read')
                buffer.extend(content)
                remaining -= len(content)
    return LeasedCommandResult(status if status >= 0 else 128 - status, bytes(output[0]), bytes(output[1]),
                               started_at, finished_at, finished - started)


CARGO_START_ERROR = 'The guest could not start Cargo because the executable is missing from the prepared PATH'


def run_slot(execution: LeasedSlotExecution, argv: tuple[str, ...], environment: Mapping[str, str], *,
             shell: ShellEnvironment, compiler_version: str, free_floor_gib: float,
             output_max_bytes: int, timeout_s: float, build_start_fd: int = -1) -> LeasedCommandResult:
    """Check shell and compiler parity before running the exact judgment command."""
    slot = execution.slot
    descriptor = build_folders.build_folder_lock_fd(slot.path)
    if descriptor not in execution.descriptors:
        raise ValueError('The command lacks its maker slot descriptor')
    if (argv[:2] not in (('cargo', 'check'), ('cargo', 'clippy'))
            or any(word == '--fix' or word.startswith('--fix=') for word in argv)
            or any('\0' in word for word in argv)):
        raise ValueError('The guest accepts only Cargo judgment commands')
    for index, word in enumerate(argv):
        if word == '--target-dir':
            if index + 1 >= len(argv):
                raise ValueError('The Cargo target directory is missing')
            target = (slot.path / argv[index + 1]).resolve()
        elif word.startswith('--target-dir='):
            target = (slot.path / word.partition('=')[2]).resolve()
        else:
            continue
        if not target.is_relative_to(slot.path.resolve()) and target != slot.cargo_dir.resolve():
            raise ValueError('The command output escapes its maker slot')
    command_environment = {**os.environ, **environment, **slot.env,
        'CARGO_TARGET_DIR': str(slot.path / 'target'), 'BUILD_FOLDER': str(slot.path),
        'BUILD_FOLDER_LOCK_FD': str(descriptor)}
    if min(shutil.disk_usage(slot.path).free, shutil.disk_usage(slot.cargo_dir).free) < free_floor_gib * 1024**3:
        raise ValueError('The command lacks the required free disk space')
    with build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(
            '.leased-step-', build_folders.ExplicitBuildParent(slot.cargo_dir))) as stage:
        started = stage / 'started'
        report_start = f"printf '\\n' >&{build_start_fd} || exit 75\n" if build_start_fd >= 0 else ''
        close_report = f' {build_start_fd}>&-' if build_start_fd >= 0 else ''
        assignments = ' '.join(f'{name}={shlex.quote(value)}' for name, value in
                               {**environment, **slot.env, 'CARGO_TARGET_DIR': str(slot.path / 'target')}.items())
        script = (shell_environment_script(shell)
            + '\nunset RUSTUP_TOOLCHAIN\n'
            + toolchain_install_script('rustc ' + compiler_version, 'hana') + '\n'
            + 'actual=$(rustc -V) || exit 75\n'
            + f'[[ "$actual" == {shlex.quote("rustc " + compiler_version)} '
            + f'|| "$actual" == {shlex.quote("rustc " + compiler_version + " ")}* ]] || exit 75\n'
            + f'export {assignments}\n'
            + 'cargo=$(type -P -- cargo) && [[ -x "$cargo" && ! -d "$cargo" ]] || '
            + f'{{ printf "%s\\n" {shlex.quote(CARGO_START_ERROR)} >&2; exit 127; }}\n'
            + f': > {shlex.quote(str(started))} || exit 75\n'
            + report_start + f'exec -a cargo -- "$cargo" {shlex.join(argv[1:])}{close_report}')
        result = capture_leased_command(['bash', '-e', '-c', script], execution,
                                        command_environment, output_max_bytes, timeout_s=timeout_s,
                                        extra_descriptors=(build_start_fd,) if build_start_fd >= 0 else ())
        if not started.exists():
            reason = (CARGO_START_ERROR if result.stderr.endswith((CARGO_START_ERROR + '\n').encode())
                      else 'The guest shell or compiler parity check failed')
            raise ValueError(reason)
        return result


def positive_setting(values: dict[str, str], key: str) -> float:
    value = float(values[key])
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{key}: expected a positive number')
    return value


def shell_environment_script(shell: ShellEnvironment) -> str:
    """Keep the caller's tools after the development shell's tools."""
    return ('linux_runner_path=$PATH\n'
            + f'source {shlex.quote(str(shell.exports))} || exit 75\n'
            + 'export PATH="$PATH:$linux_runner_path"\n'
            + 'unset linux_runner_path\n'
            + 'if [[ -n ${shellHook-} ]]; then eval "$shellHook" || exit 75; fi\n')


def normalize_tool_version(value: str) -> str:
    """Keep each nonempty version line in one common form."""
    return ' '.join(line.strip() for line in value.splitlines() if line.strip())


class SlotRun(TypedDict):
    """A process group that owns one slot until every process ends."""

    pid: int
    start_time: int
    process_group: int
    token: str
    boot_id: NotRequired[str]


@dataclass(frozen=True)
class UnmarkedSlot:
    """The slot has no process marker."""


def read_slot_run(marker: Path) -> SlotRun | UnmarkedSlot:
    if not marker.exists():
        return UnmarkedSlot()
    raw = cast(object, json.loads(marker.read_text()))
    if not isinstance(raw, dict):
        raise ValueError('invalid slot process marker')
    fields = cast(dict[str, object], raw)
    for key in ('pid', 'start_time', 'process_group'):
        if type(fields.get(key)) is not int or cast(int, fields[key]) < 1:
            raise ValueError('invalid slot process identity')
    if not isinstance(fields.get('token'), str) or not fields['token']:
        raise ValueError('invalid slot process token')
    lease: SlotRun = {'pid': cast(int, fields['pid']), 'start_time': cast(int, fields['start_time']),
                      'process_group': cast(int, fields['process_group']), 'token': cast(str, fields['token'])}
    if 'boot_id' in fields:
        identity = fields['boot_id']
        if not isinstance(identity, str) or not identity:
            raise ValueError('invalid slot boot identity')
        lease['boot_id'] = identity
    return lease


@dataclass(frozen=True)
class SlotProcessSelection:
    """Confirmed slot processes and evidence that prevents release."""

    pids: tuple[int, ...]
    unresolved: bool


class SlotProcessesUnresolved(RuntimeError):
    """Process evidence cannot confirm that the slot has ended."""


class SlotStartupRefused(RuntimeError):
    """A slot has live processes or unresolved process ownership."""


SLOT_PROCESSES_UNRESOLVED = 3


@dataclass(frozen=True)
class ProcessIdentity:
    """Public process fields that remain readable for non-dumpable processes."""

    real_uid: int
    parent: int
    group: int
    start_time: int
    state: str


def process_identity(pid: int, proc_root: Path) -> ProcessIdentity:
    fields = process_stat.process_fields(pid, proc_root)
    status = (proc_root / str(pid) / 'status').read_text()
    uid = re.search(r'^Uid:\s+(\d+)\b', status, re.MULTILINE)
    if uid is None:
        raise ValueError('process real UID is unavailable')
    return ProcessIdentity(int(uid[1]), int(fields[1]), int(fields[2]), int(fields[19]), fields[0])


def select_slot_processes(mirror: Path, proc_root: Path = PROC_ROOT, *,
                          require_boot_identity: bool = False) -> SlotProcessSelection:
    """Read each ownership source independently before deciding uncertainty."""
    lease = read_slot_run(mirror / '.linux-run.json')
    if not isinstance(lease, UnmarkedSlot):
        current_boot = boot_identity(proc_root) if 'boot_id' in lease or require_boot_identity else ''
        if slot_boot(lease, current_boot, require_boot_identity=require_boot_identity) is SlotBoot.PREVIOUS:
            return SlotProcessSelection((), False)
    selected: set[int] = set()
    parents: dict[int, int] = {}
    uncertain: set[int] = set()
    root = mirror.resolve()
    own_pid = os.getpid()
    own_uid = os.getuid()
    identities: dict[int, ProcessIdentity] = {}
    for process in proc_root.glob('[0-9]*'):
        pid = int(process.name)
        try:
            identities[pid] = process_identity(pid, proc_root)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            if not isinstance(lease, UnmarkedSlot):
                uncertain.add(pid)
    # The checking process and its ancestors cannot be leftovers of a lost run.
    checking_chain: set[int] = set()
    ancestor = own_pid
    while ancestor in identities and ancestor not in checking_chain:
        checking_chain.add(ancestor)
        ancestor = identities[ancestor].parent
    for pid, identity in identities.items():
        if identity.real_uid != own_uid or identity.state in ('Z', 'X'):
            continue
        if not isinstance(lease, UnmarkedSlot):
            if pid == lease['pid']:
                if (identity.start_time != lease['start_time']
                        or identity.group != lease['process_group']):
                    raise ValueError('slot process identity changed')
                selected.add(pid)
                parents[pid] = identity.parent
                continue
            if identity.start_time < lease['start_time']:
                continue
        if pid in checking_chain:
            continue
        # Slot children keep their user; only an orphan can acquire a root parent (PID 1).
        parent = identities.get(identity.parent)
        if identity.parent != 1 and parent is not None and parent.real_uid != own_uid:
            continue
        parents[pid] = identity.parent
        process = proc_root / str(pid)
        denied = False
        try:
            command = (process / 'cmdline').read_bytes().split(b'\0')
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            command = []
            denied = True
        runner = any(word.endswith(b'/linux_runner.py') for word in command)
        if runner and b'--mirror' in command:
            index = command.index(b'--mirror') + 1
            if index < len(command):
                path = Path(os.fsdecode(command[index]))
                invocation_root = path if path.is_absolute() else Path.home() / path
                if invocation_root.resolve() == root:
                    selected.add(pid)
        if not isinstance(lease, UnmarkedSlot):
            try:
                environment = (process / 'environ').read_bytes().split(b'\0')
                token = f"BUILD_TARGET_RUN_TOKEN={lease['token']}".encode()
                if token in environment:
                    selected.add(pid)
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                denied = True
        try:
            cwd = (process / 'cwd').resolve(strict=True)
            if cwd == root or root in cwd.parents:
                selected.add(pid)
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            denied = True
        if denied and pid not in selected and not isinstance(lease, UnmarkedSlot):
            # Confirm that missing evidence does not belong to an exited process.
            try:
                if process_stat.process_fields(pid, proc_root)[0] in ('Z', 'X'):
                    continue
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                pass
            uncertain.add(pid)
    # A parent chain proves ownership even when a child hides its environment.
    changed = True
    while changed:
        children = {pid for pid, parent in parents.items() if parent in selected}
        changed = not children.issubset(selected)
        selected.update(children)
    uncertain.difference_update(selected)
    selected.discard(own_pid)
    return SlotProcessSelection(tuple(sorted(selected)), bool(uncertain))


def slot_processes(mirror: Path, proc_root: Path = PROC_ROOT, *,
                   require_boot_identity: bool = False) -> tuple[int, ...]:
    """Return only confirmed processes, or refuse uncertain cleanup."""
    selection = select_slot_processes(mirror, proc_root, require_boot_identity=require_boot_identity)
    if selection.unresolved:
        raise SlotProcessesUnresolved('slot process ownership remains unresolved')
    if not selection.pids:
        (mirror / '.linux-run.json').unlink(missing_ok=True)
    return selection.pids


def print_slot_processes(mirror: Path, *, require_boot_identity: bool = False) -> int:
    """Refuse an uncertain external selection without a traceback or kill list."""
    try:
        pids = slot_processes(mirror, require_boot_identity=require_boot_identity)
    except SlotProcessesUnresolved:
        return SLOT_PROCESSES_UNRESOLVED
    except (OSError, ValueError):
        return 1
    if pids:
        print(*pids, sep='\n')
    return 0


@contextmanager
def slot_run(mirror: Path, *, require_boot_identity: bool = False) -> Generator[None, None, None]:
    """Publish ownership before shell evaluation can start a child process."""
    try:
        if ((mirror / '.linux-run.json').exists()
                and slot_processes(mirror, require_boot_identity=require_boot_identity)):
            raise SlotStartupRefused('the slot still has running processes')
    except (OSError, ValueError, SlotProcessesUnresolved) as error:
        raise SlotStartupRefused('an earlier run still has unresolved ownership') from error
    pid = os.getpid()
    token = uuid.uuid4().hex
    lease: SlotRun = {'pid': pid, 'start_time': int(process_stat.process_fields(pid)[19]),
                      'process_group': os.getpgrp(), 'token': token, 'boot_id': boot_identity()}
    marker = mirror / '.linux-run.json'
    with marker.open('x') as output:
        _ = output.write(json.dumps(lease))
    previous = os.environ.get('BUILD_TARGET_RUN_TOKEN', '')
    os.environ['BUILD_TARGET_RUN_TOKEN'] = token
    try:
        yield
    finally:
        if previous:
            os.environ['BUILD_TARGET_RUN_TOKEN'] = previous
        else:
            del os.environ['BUILD_TARGET_RUN_TOKEN']
        # Keep the marker when a detached child still owns the slot.
        try:
            children = slot_processes(mirror, require_boot_identity=require_boot_identity)
        except (OSError, ValueError, SlotProcessesUnresolved):
            print('mac_cleanup=unconfirmed', flush=True)
        else:
            if children:
                print('mac_cleanup=unconfirmed', flush=True)
            else:
                marker.unlink(missing_ok=True)


def mirror_selector_script(mirror: str, *, require_boot_identity: bool = False) -> str:
    """Select the runner and its children even outside the slot directory."""
    code = ("import sys; from pathlib import Path; "
            + "from scripts.mac_test.linux_runner import print_slot_processes; "
            + f"sys.exit(print_slot_processes(Path(sys.argv[1]), require_boot_identity={require_boot_identity!r}))")
    return ('select_leftovers() {\n'
            + f'    "$HOME/.claude/scripts/lib/py" -c {shlex.quote(code)} "$HOME"/{shlex.quote(mirror)}\n'
            + '}\n')


def nextest_words_with_skip(
    words: tuple[str, ...], skip_expression: str, *, insert_filter: bool = False
) -> tuple[str, ...]:
    if not skip_expression:
        return words
    updated = list(words)
    boundary = updated.index("--") if "--" in updated else len(updated)
    for index, word in enumerate(updated[:boundary]):
        if not insert_filter and word != '-E':
            continue
        if word in ("-E", "--filter-expr", "--filterset"):
            if index + 1 >= boundary:
                return words
            updated[index + 1] = f"({updated[index + 1]}) & not ({skip_expression})"
            return tuple(updated)
        for prefix in ("--filter-expr=", "--filterset=", "-E=", "-E"):
            if word.startswith(prefix) and word != prefix:
                updated[index] = f"{prefix}({word[len(prefix):]}) & not ({skip_expression})"
                return tuple(updated)
    if insert_filter:
        updated[boundary:boundary] = ["-E", f"not ({skip_expression})"]
    return tuple(updated)


def output_filesystem_paths(checkout: Path, environment: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Resolve the checkout and its Cargo output filesystems."""
    outputs = read_cargo_output_roots(checkout, environment=environment)
    if isinstance(outputs, CargoOutputUnavailable):
        if not (checkout / "Cargo.toml").exists():
            return (checkout,)
        raise OSError(outputs.reason)
    return (checkout, outputs.target_directory, outputs.build_directory)


def output_free_bytes(checkout: Path, environment: Mapping[str, str] | None = None) -> int:
    readings: list[int] = []
    for path in output_filesystem_paths(checkout, environment):
        while True:
            try:
                readings.append(shutil.disk_usage(path)[2])
                break
            except FileNotFoundError:
                if path == path.parent:
                    raise
                path = path.parent
    return min(readings)


def toolchain_install_script(expected_rustc: str, target: str = 'linux') -> str:
    """Install the tree's toolchain under one lock for its channel."""
    version = re.fullmatch(r'rustc (\d+\.\d+\.\d+)(?: .*)?', expected_rustc)
    fallback = (f'export RUSTUP_TOOLCHAIN={shlex.quote(version[1])}' if version else ':')
    # Bash decides the inherited, file and fallback cases; Python only parses a toolchain file.
    parse_code = '\n'.join((
        'import hashlib, sys, tomllib',
        'from pathlib import Path',
        'text = Path(sys.argv[1]).read_text()',
        'try:',
        '    print(tomllib.loads(text)["toolchain"].get("channel") or hashlib.sha256(text.encode()).hexdigest())',
        'except tomllib.TOMLDecodeError:',
        '    print(text.strip())',
    ))
    return '\n'.join((
        'toolchain=${RUSTUP_TOOLCHAIN-}',
        'if [[ -z $toolchain ]]; then',
        '  toolchain_file=""',
        '  if [[ -f rust-toolchain ]]; then toolchain_file=rust-toolchain',
        '  elif [[ -f rust-toolchain.toml ]]; then toolchain_file=rust-toolchain.toml',
        '  fi',
        '  if [[ -n $toolchain_file ]]; then',
        f'    toolchain=$(python3 -c {shlex.quote(parse_code)} "$toolchain_file") || exit 75',
        '  else',
        f'    {fallback}',
        '    toolchain=${RUSTUP_TOOLCHAIN-}',
        '  fi',
        'fi',
        'if [[ -n "$toolchain" ]]; then',
        '  mkdir -p -- "${RUSTUP_HOME:-$HOME/.rustup}" || exit 75',
        '  (',
        '    flock -x 9 || exit 75',
        '    if install_output=$(rustup toolchain install --no-self-update --no-update 2>&1); then',
        '      if [[ -n "$install_output" ]]; then printf \'%s\\n\' "$install_output"; fi',
        '    else',
        '      install_output=${install_output//$\'\\n\'/ }',
        f'      printf \'%s version mismatch: toolchain install failed: %s\\n\' {shlex.quote(target)} "$install_output" >&2',
        '      exit 75',
        '    fi',
        '  ) 9>"${RUSTUP_HOME:-$HOME/.rustup}/$toolchain.lock" || exit 75',
        'fi',
    ))

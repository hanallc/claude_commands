"""Render independent safety services for the kept Hana guest."""
from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING, ClassVar, cast

if TYPE_CHECKING:
    from scripts.lib import conf


CONTROL = Path('/var/lib/hana-aws')
IMAGE_RUNTIME = Path('/var/lib/hana-runtime')
DATA = Path('/var/lib/hana')
RUNNER_HOME = DATA / 'runner'
PERMIT = Path('/run/hana-aws/active-work-permit.json')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
WORK_SLICE = 'hana-work.slice'
# NixOS gives a raw unit no system PATH; the guest programs run systemctl and systemd-run from here.
SYSTEM_BIN = '/run/current-system/sw/bin'


@dataclass(frozen=True)
class PermitWindow:
    enabled: bool
    permit_max_seconds: int


def read_permit_window(configuration: conf.ConfigRead, *, recover_invalid: bool = True) -> PermitWindow:
    """Read the Hana switch through aws_switch: an unattended reader counts an invalid value as off.

    The image build passes recover_invalid=False and refuses an invalid value, so no image holds a stale switch."""
    from scripts.aws_target.config import AwsOn, AwsSwitchUnusable, aws_switch
    from scripts.lib import conf

    switch = aws_switch(configuration)
    if isinstance(switch, AwsSwitchUnusable) and not recover_invalid:
        raise conf.Unusable(switch.reason)
    return PermitWindow(isinstance(switch, AwsOn),
                        configuration.value('permit_max_seconds', conf.positive_whole, recover_invalid=recover_invalid))


@dataclass(frozen=True)
class SafetySettings:
    enabled: bool
    guard_grace_seconds: int
    watchdog_check_seconds: int
    permit_max_seconds: int
    step_cpu_limit: int
    step_memory_gib: int
    mount_timeout_seconds: int


def decode_settings(values: Mapping[str, object]) -> SafetySettings:
    """Validate the settings used by the installed guest services."""
    enabled = values['enabled']
    if type(enabled) is not bool:
        raise ValueError('enabled must be a boolean')
    numbers: dict[str, int] = {}
    for key in ('guard_grace_seconds', 'watchdog_check_seconds', 'permit_max_seconds', 'step_cpu_limit',
                'step_memory_gib', 'mount_timeout_seconds'):
        number = values[key]
        if type(number) is not int or number <= 0:
            raise ValueError(key + ' must be a positive integer')
        numbers[key] = number
    return SafetySettings(enabled, **numbers)


def read_settings(configuration: conf.ConfigRead, *, recover_invalid: bool = True) -> SafetySettings:
    """Read Hana settings without reading deployment or credential files."""
    from scripts.lib import conf

    permit = read_permit_window(configuration, recover_invalid=recover_invalid)
    values: dict[str, object] = {'enabled': permit.enabled, 'permit_max_seconds': permit.permit_max_seconds}
    for key in ('guard_grace_seconds', 'watchdog_check_seconds', 'step_cpu_limit',
                'step_memory_gib', 'mount_timeout_seconds'):
        values[key] = configuration.value(key, conf.positive_whole, recover_invalid=recover_invalid)
    return decode_settings(values)


@dataclass(frozen=True)
class WorkPermit:
    """Permission for one boot and execution generation until a UTC epoch time."""

    expires_at: float
    generation: str
    boot_id: str


class PermitRefusal(StrEnum):
    NOT_INSTALLED = 'no permit record is installed'
    UNREADABLE = 'no valid permit record is readable'
    BOOT_MISMATCH = 'the permit does not match the current boot'
    INVALID_EXPIRY = 'the permit expiry is invalid'
    WALL_CLOCK_EXPIRED = 'the wall-clock expiry has passed'
    OUTSIDE_WINDOW = 'the expiry exceeds the permit renewal window'
    DEADLINE_UNREADABLE = 'the saved boot-clock deadline is unreadable'
    BOOT_CLOCK_EXPIRED = 'the boot-clock deadline has passed'


@dataclass(frozen=True)
class NoWorkPermit:
    """The guest cannot use the permit record."""
    reason: PermitRefusal = PermitRefusal.UNREADABLE


Permit = WorkPermit | NoWorkPermit


def _generation_number(generation: str) -> int:
    if generation.isascii() and generation.isdecimal():
        number = int(generation)
        if number > 0:
            return number
    raise ValueError('The Hana permit generation is invalid')


def read_permit(path: Path) -> Permit:
    """Read the permit and record the reason if the guest cannot use it."""
    try:
        raw = cast(object, json.loads(path.read_text()))
        if not isinstance(raw, dict):
            return NoWorkPermit()
        row = cast(dict[str, object], raw)
        generation, boot_id = row['generation'], row['boot_id']
        if not isinstance(generation, str):
            return NoWorkPermit()
        _ = _generation_number(generation)
        if not isinstance(boot_id, str) or not boot_id.strip():
            return NoWorkPermit()
        expires = row.get('expires_at')
        if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not math.isfinite(expires) or expires <= 0):
            return NoWorkPermit(PermitRefusal.INVALID_EXPIRY)
        return WorkPermit(float(expires), generation, boot_id)
    except FileNotFoundError:
        return NoWorkPermit(PermitRefusal.NOT_INSTALLED)
    except OverflowError:
        return NoWorkPermit(PermitRefusal.INVALID_EXPIRY)
    except (OSError, UnicodeError, ValueError, KeyError):
        return NoWorkPermit()


def check_permit(settings: SafetySettings, permit: Permit, boot_id: str,
                 now: float) -> WorkPermit | PermitRefusal:
    """Require the current identity and a bounded wall-clock expiry."""
    if isinstance(permit, NoWorkPermit):
        return permit.reason
    if not boot_id or permit.boot_id != boot_id:
        return PermitRefusal.BOOT_MISMATCH
    if not math.isfinite(permit.expires_at) or permit.expires_at <= 0:
        return PermitRefusal.INVALID_EXPIRY
    remaining = permit.expires_at - now
    if remaining <= 0:
        return PermitRefusal.WALL_CLOCK_EXPIRED
    if not remaining <= settings.permit_max_seconds:
        return PermitRefusal.OUTSIDE_WINDOW
    return permit


@dataclass(frozen=True)
class GuestPaths:
    """Root-owned coordination files shared by safety, renewal, and command admission."""

    directory: Path
    boot_id: Path

    @property
    def permit(self) -> Path:
        return self.directory / PERMIT.name

    @property
    def lock(self) -> Path:
        return self.directory / 'permit.lock'

    @property
    def fence(self) -> Path:
        return self.directory / 'shutdown.json'

    @property
    def deadline(self) -> Path:
        return self.directory / 'permit-deadline.json'


GUEST_PATHS = GuestPaths(PERMIT.parent, BOOT_ID)


@contextmanager
def guest_lock(paths: GuestPaths) -> Generator[None]:
    """Hold one lock across permit reads, shutdown decisions, renewal, and command starts."""
    paths.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(paths.lock, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def atomic_record(path: Path, value: Mapping[str, object]) -> None:
    """Publish a complete private record through a sibling file and atomic rename."""
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(dict(value), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class PermitDeadline:
    """One observed permit with a boot-clock deadline that survives reader restarts."""

    permit: WorkPermit
    monotonic_deadline: float


@dataclass(frozen=True)
class FirstPermitRead:
    """This boot has not recorded a permit deadline."""


@dataclass(frozen=True)
class PermitDeadlineUnreadable:
    """A damaged deadline cannot grant another renewal window."""


BoundPermit = PermitDeadline | PermitRefusal
PreviousDeadline = PermitDeadline | FirstPermitRead | PermitDeadlineUnreadable


def read_deadline(path: Path) -> PreviousDeadline:
    try:
        raw = cast(object, json.loads(path.read_text()))
        if not isinstance(raw, dict):
            return PermitDeadlineUnreadable()
        row = cast(dict[str, object], raw)
        expires, deadline = row['expires_at'], row['monotonic_deadline']
        generation, boot_id = row['generation'], row['boot_id']
        if (isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires)
                or isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline) or not isinstance(generation, str) or not isinstance(boot_id, str)):
            return PermitDeadlineUnreadable()
        return PermitDeadline(WorkPermit(float(expires), generation, boot_id), float(deadline))
    except FileNotFoundError:
        return FirstPermitRead()
    except (OSError, UnicodeError, ValueError, KeyError, OverflowError):
        return PermitDeadlineUnreadable()


def observe_permit(settings: SafetySettings, permit: Permit, previous: PreviousDeadline,
                   boot_id: str, *, clock: Callable[[], float],
                   uptime: Callable[[], float]) -> BoundPermit:
    """Bound each new permit read without extending an unchanged permit's boot-clock deadline."""
    if isinstance(previous, PermitDeadlineUnreadable):
        return PermitRefusal.DEADLINE_UNREADABLE
    boot_time = uptime()
    now = clock()
    checked = check_permit(settings, permit, boot_id, now)
    if isinstance(checked, PermitRefusal):
        return checked
    if isinstance(previous, PermitDeadline) and previous.permit == checked:
        return previous if boot_time < previous.monotonic_deadline else PermitRefusal.BOOT_CLOCK_EXPIRED
    return PermitDeadline(checked, boot_time + (checked.expires_at - now))


def bound_permit(settings: SafetySettings, paths: GuestPaths, *, clock: Callable[[], float],
                 uptime: Callable[[], float]) -> BoundPermit:
    """Read and record permission while the caller holds the guest lock."""
    previous = read_deadline(paths.deadline)
    permit = observe_permit(settings, read_permit(paths.permit), previous,
        read_identity(paths.boot_id), clock=clock, uptime=uptime)
    if isinstance(permit, PermitDeadline) and permit != previous:
        atomic_record(paths.deadline, {**asdict(permit.permit), 'monotonic_deadline': permit.monotonic_deadline})
    return permit


def write_permit(settings: SafetySettings, permit: WorkPermit, *, expected_generation: int,
                 paths: GuestPaths = GUEST_PATHS,
                 clock: Callable[[], float] = time.time) -> None:
    """Install a permit or confirm an exact replay under the guest lock."""
    generation = _generation_number(permit.generation)
    with guest_lock(paths):
        if paths.fence.exists():
            raise ValueError('Hana shutdown is fenced')
        boot = read_identity(paths.boot_id)
        current = read_permit(paths.permit)
        if isinstance(check_permit(settings, permit, boot, clock()), PermitRefusal):
            raise ValueError('The Hana permit identity or renewal window is invalid')
        if current == permit:
            return
        if isinstance(current, WorkPermit):
            if current.boot_id != boot:
                raise ValueError('The installed Hana permit belongs to another boot')
            previous = int(current.generation)
        elif current.reason is PermitRefusal.NOT_INSTALLED:
            previous = 0
        else:
            raise ValueError('The installed Hana permit is unreadable')
        if type(expected_generation) is not int or expected_generation != previous:
            raise ValueError('The Hana permit generation changed')
        if generation <= previous:
            raise ValueError('The Hana permit generation did not increase')
        atomic_record(paths.permit, asdict(permit))


class GuardAction(StrEnum):
    WAIT = 'wait'
    RELEASE = 'release'
    STOP = 'stop'


def guard_action(settings: SafetySettings, permit: BoundPermit, *, boot_elapsed: float,
                 clock: Callable[[], float], uptime: Callable[[], float]) -> GuardAction:
    """Allow setup after a fresh permit, or stop after the boot grace interval."""
    if watchdog_action(settings, permit, clock=clock, uptime=uptime) == WatchdogAction.KEEP:
        return GuardAction.RELEASE
    return GuardAction.WAIT if boot_elapsed < settings.guard_grace_seconds else GuardAction.STOP


class WatchdogAction(StrEnum):
    KEEP = 'keep'
    STOP = 'stop'


def watchdog_action(settings: SafetySettings, permit: BoundPermit, *, clock: Callable[[], float],
                    uptime: Callable[[], float]) -> WatchdogAction:
    """Decide without reading controller or command state."""
    checked = check_deadline(settings, permit, clock=clock, uptime=uptime)
    return WatchdogAction.KEEP if isinstance(checked, PermitDeadline) else WatchdogAction.STOP


def check_deadline(settings: SafetySettings, permit: BoundPermit, *, clock: Callable[[], float],
                   uptime: Callable[[], float]) -> BoundPermit:
    if isinstance(permit, PermitRefusal):
        return permit
    work = permit.permit
    checked = check_permit(settings, work, work.boot_id, clock())
    if isinstance(checked, PermitRefusal):
        return checked
    return permit if uptime() < permit.monotonic_deadline else PermitRefusal.BOOT_CLOCK_EXPIRED


def stop_work(run: Callable[[list[str]], None]) -> None:
    """Kill every command cgroup before requesting poweroff, including after a kill error."""
    try:
        run(['systemctl', 'kill', '--kill-whom=all', '--signal=KILL', WORK_SLICE])
    finally:
        run(['systemctl', 'poweroff', '--no-block'])


def _command_arguments(settings: SafetySettings, generation: str,
                       command: tuple[str, ...], directory: Path) -> list[str]:
    """Prepare an unprivileged command in its own bounded slice after admission checks."""
    if not re.fullmatch(r'[A-Za-z0-9_]+', generation):
        raise ValueError('The generation cannot name a command unit')
    if not directory.is_relative_to(DATA) or '..' in directory.parts or not command:
        raise ValueError('The command needs a source directory on the data mount')
    return ['systemd-run', '--wait', '--collect', '--uid=hana-runner',
            '--unit=hana-command-' + generation, '--slice=hana-work-' + generation + '.slice',
            '--property=CPUQuota=' + str(settings.step_cpu_limit * 100) + '%',
            '--property=MemoryMax=' + str(settings.step_memory_gib * 1024**3),
            '--property=KillMode=control-group', '--working-directory=' + str(directory), '--', *command]


class CommandAdmissionRefusalCode(StrEnum):
    ADMISSION_DISABLED = 'admission-disabled'
    DATA_NOT_MOUNTED = 'data-not-mounted'
    PERMIT_REJECTED = 'permit-rejected'
    RUNTIME_ABI_MISMATCH = 'runtime-abi-mismatch'
    PRE_COMMAND_FAILED = 'pre-command-failed'


class GuestAdmissionDisabled(ValueError):
    code: ClassVar[CommandAdmissionRefusalCode] = CommandAdmissionRefusalCode.ADMISSION_DISABLED

    def __init__(self) -> None:
        super().__init__('Hana command admission is disabled on the guest')


class GuestDataNotMounted(ValueError):
    code: ClassVar[CommandAdmissionRefusalCode] = CommandAdmissionRefusalCode.DATA_NOT_MOUNTED

    def __init__(self) -> None:
        super().__init__('The Hana data directory is not mounted')


class GuestPermitRefused(ValueError):
    code: ClassVar[CommandAdmissionRefusalCode] = CommandAdmissionRefusalCode.PERMIT_REJECTED

    def __init__(self, reason: PermitRefusal) -> None:
        super().__init__(f'The Hana permit is not fresh: {reason}')
        self.reason: PermitRefusal = reason


class GuestRuntimeAbiMismatch(ValueError):
    code: ClassVar[CommandAdmissionRefusalCode] = CommandAdmissionRefusalCode.RUNTIME_ABI_MISMATCH

    def __init__(self, requested: int, installed: int) -> None:
        super().__init__(f'The Hana runtime ABI {requested} differs from the image ABI {installed}')


def admit_command[StartedCommand](settings: SafetySettings, command: tuple[str, ...], directory: Path,
                                  *, mounted: bool, start: Callable[[list[str]], StartedCommand],
                                  paths: GuestPaths = GUEST_PATHS, clock: Callable[[], float] = time.time,
                                  uptime: Callable[[], float] = time.monotonic) -> StartedCommand:
    """Start a command under the same lock that excludes shutdown and renewal races.

    The start callback spawns the process and returns immediately.
    The caller waits for completion outside the lock.
    """
    with guest_lock(paths):
        if paths.fence.exists():
            raise ValueError('Shutdown is fenced; command admission is refused')
        if not settings.enabled:
            raise GuestAdmissionDisabled()
        if not mounted:
            raise GuestDataNotMounted()
        permit = check_deadline(settings, bound_permit(settings, paths, clock=clock, uptime=uptime),
                               clock=clock, uptime=uptime)
        if isinstance(permit, PermitRefusal):
            raise GuestPermitRefused(permit)
        return start(_command_arguments(settings, permit.permit.generation, command, directory))


def read_identity(path: Path) -> str:
    """An unreadable identity cannot authorize a permit."""
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return ''


def run_safety(mode: str, settings: SafetySettings, *, clock: Callable[[], float] = time.time,
               uptime: Callable[[], float] = time.monotonic,
               pause: Callable[[float], None] = time.sleep,
               run: Callable[[list[str]], None], paths: GuestPaths = GUEST_PATHS) -> None:
    """Keep boot grace separate from the watchdog's execution deadline."""
    if mode not in ('guard', 'watchdog'):
        raise ValueError('Expected guard or watchdog')
    while True:
        delay = 0.0
        permit: BoundPermit = PermitRefusal.UNREADABLE
        with guest_lock(paths):
            if paths.fence.exists():
                stopping = True
            else:
                permit = bound_permit(settings, paths, clock=clock, uptime=uptime)
                if mode == 'guard':
                    action = guard_action(settings, permit, boot_elapsed=uptime(), clock=clock, uptime=uptime)
                    if action == GuardAction.RELEASE:
                        return
                    stopping = action == GuardAction.STOP
                else:
                    stopping = watchdog_action(settings, permit, clock=clock, uptime=uptime) == WatchdogAction.STOP
            if stopping:
                atomic_record(paths.fence, {'state': 'shutdown'})
            elif mode == 'guard':
                delay = min(settings.watchdog_check_seconds, max(0, settings.guard_grace_seconds - uptime()))
            else:
                assert isinstance(permit, PermitDeadline)
                delay = min(settings.watchdog_check_seconds, max(0, permit.permit.expires_at - clock()),
                            max(0, permit.monotonic_deadline - uptime()))
        if stopping:
            stop_work(run)
            return
        pause(delay)


@dataclass(frozen=True)
class GuestFile:
    path: Path
    content: str
    mode: int


def guest_program() -> str:
    """Load safety from the installed image closure."""
    return f'''import sys
sys.path.insert(0, {str(IMAGE_RUNTIME)!r})
from scripts.aws_target.hana_guest import installed_main
if __name__ == '__main__':
    installed_main(sys.argv[1])
'''


def installed_main(mode: str) -> None:
    raw = cast(object, json.loads((CONTROL / 'safety.json').read_text()))
    if not isinstance(raw, dict):
        raise ValueError('Hana safety settings must be an object')
    settings = decode_settings(cast(dict[str, object], raw))

    def run(command: list[str]) -> None:
        _ = subprocess.run(command, check=True, timeout=settings.watchdog_check_seconds)

    run_safety(mode, settings, run=run)


def render_files(settings: SafetySettings, *, device: Path, tool_setup: Path | None = None) -> tuple[GuestFile, ...]:
    """Return guest files without installing units or touching deployment state."""
    for path in (device, tool_setup):
        if path is None:
            continue
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path)) or '..' in path.parts:
            raise ValueError('Expected an absolute device or setup path')
    prefix = f'{SYSTEM_BIN}/python3 -I {CONTROL}/safety.py'
    guard = f'''[Unit]
Description=Hana boot permit guard
DefaultDependencies=no
FailureAction=poweroff

[Service]
Type=oneshot
RemainAfterExit=yes
RuntimeDirectory=hana-aws
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
Environment=PATH={SYSTEM_BIN}
ExecStart={prefix} guard
TimeoutStartSec={settings.guard_grace_seconds + settings.watchdog_check_seconds}s

[Install]
WantedBy=multi-user.target
'''
    watchdog = f'''[Unit]
Description=Hana execution permit watchdog
After=hana-guard.service
FailureAction=poweroff
StartLimitAction=poweroff

[Service]
Type=simple
Environment=PATH={SYSTEM_BIN}
ExecStart={prefix} watchdog
Restart=always
RestartSec={settings.watchdog_check_seconds}s

[Install]
WantedBy=multi-user.target
'''
    mount = f'''[Unit]
Description=Hana data disk
JobTimeoutSec={settings.mount_timeout_seconds}s

[Mount]
What={device}
Where={DATA}
Type=ext4
Options=nofail
TimeoutSec={settings.mount_timeout_seconds}s

[Install]
WantedBy=multi-user.target
'''
    tools = f'''[Unit]
Description=Hana tool setup
Requires=var-lib-hana.mount
After=hana-guard.service var-lib-hana.mount
ConditionPathIsMountPoint={DATA}

[Service]
Type=oneshot
ExecStart={tool_setup}
RemainAfterExit=yes
'''
    work_slice = f'''[Unit]
Description=Hana command resource bounds

[Slice]
CPUQuota={settings.step_cpu_limit * 100}%
MemoryMax={settings.step_memory_gib * 1024**3}
'''
    units = {'hana-guard.service': guard, 'hana-watchdog.service': watchdog, 'var-lib-hana.mount': mount}
    if tool_setup is not None:
        units['hana-tools.service'] = tools
    units[WORK_SLICE] = work_slice
    return (GuestFile(CONTROL / 'safety.py', guest_program(), 0o600),
            GuestFile(CONTROL / 'safety.json', json.dumps(asdict(settings)) + '\n', 0o600),
            *(GuestFile(Path('/etc/systemd/system') / name, content, 0o644) for name, content in units.items()))

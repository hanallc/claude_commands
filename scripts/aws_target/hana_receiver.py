"""Receive signed control objects as root without guest AWS credentials."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
import fcntl
import hashlib
from http.client import HTTPException, HTTPResponse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from scripts.aws_target import hana_guest as guest
from scripts.aws_target import hana_payload, hana_protocol, hana_result
from scripts.build_queue import execution_identity
from scripts.lib import build_folder_lock, process_stat

if TYPE_CHECKING:
    from scripts.lib import conf

CAPTURE_COMMAND = (
    guest.SYSTEM_BIN + '/bash', '-o', 'pipefail', '-c',
    """
capture() { head -c "$1"; cat >/dev/null; }
limit=$1
shift
"$@" 2> >(capture 8192 >&2) | capture "$limit"
""", 'hana-capture',
)
COMMAND_OUTPUT_MAX_BYTES = hana_result.RESULT_MAX_BYTES + len(b'{"finished":}') + 1
BOOTSTRAP_COMMAND = (
    guest.SYSTEM_BIN + '/python3', '-I', '-c',
    f'import sys; sys.path.insert(0, {str(guest.IMAGE_RUNTIME)!r}); '
    + 'from scripts.aws_target.hana_payload import main; '
    + 'raise SystemExit(main(int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])))',
)
MOUNTINFO = Path('/proc/self/mountinfo')
RUNNER_ENVIRONMENT = Path('/etc/hana-aws/runner-environment')
# The systemd journal identifies service deactivation for success and failure.
UNIT_EXIT_MESSAGES = ('7ad2d189f7e94e70a38c781354912448', 'd9b373ed55a64feb8242e02dbe79a49c')


@dataclass(frozen=True)
class CommandDeparture:
    terminated: bool
    lease_free: bool
    timing: hana_protocol.HanaCommandTiming = field(default_factory=hana_protocol.HanaCommandTiming)


class CommandSupervisor(Protocol):
    def mounted(self) -> bool: ...
    def start(self, envelope: hana_protocol.HanaCommandEnvelope, directory: Path, arguments: list[str]) -> execution_identity.HanaCommandAdmissionRefused | None: ...
    def stop(self, directory: Path) -> None: ...
    def departure(self, directory: Path, copy_number: int) -> CommandDeparture: ...
    def outcome(self, directory: Path) -> hana_protocol.HanaRunOutcome: ...
    def start_source_update(self, request: hana_protocol.HanaExecutionRequest, directory: Path, arguments: list[str]) -> execution_identity.HanaCommandAdmissionRefused | None: ...
    def source_outcome(self, directory: Path) -> hana_protocol.HanaSourceUpdateOutcome: ...


def data_mount_ready(mountinfo: Path = MOUNTINFO) -> bool:
    """Require the data directory to be a separate mounted filesystem."""
    try:
        return any(len(fields := line.split()) > 6 and fields[4] == str(guest.DATA)
                   for line in mountinfo.read_text().splitlines())
    except OSError:
        return False


@contextmanager
def command_lock(paths: guest.GuestPaths) -> Generator[None]:
    """Serialize command acceptance and fences outside all source leases."""
    directory = paths.directory / 'commands'
    directory.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(directory / 'accept.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def command_directory(paths: guest.GuestPaths, key: hana_result.HanaAttemptKey) -> Path:
    content = json.dumps(hana_result.encode_attempt_key(key), sort_keys=True).encode()
    return paths.directory / 'commands' / hashlib.sha256(content).hexdigest()


def command_unit(directory: Path) -> str:
    return 'hana-command-' + directory.name + '.service'


def source_update_directory(directory: Path) -> Path:
    return directory.with_name(directory.name + '-source')


class GuestCommandSupervisor:
    """Root supervises the unit while the unit owns all source and compiler processes."""

    def __init__(self, settings: ReceiverSettings, *, copy_policy: Path = hana_payload.IMAGE_COPY_POLICY,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings: ReceiverSettings = settings
        self.copy_policy: Path = copy_policy
        self.timeout_s: int = settings.aws_timeout_s
        self.clock: Callable[[], float] = clock
        self.children: dict[Path, subprocess.Popen[bytes]] = {}

    def mounted(self) -> bool:
        return data_mount_ready()

    def start(self, envelope: hana_protocol.HanaCommandEnvelope, directory: Path, arguments: list[str]) -> execution_identity.HanaCommandAdmissionRefused | None:
        return self._start(envelope.request, directory, arguments, 'execute', envelope.access.output_max_bytes)

    def start_source_update(self, request: hana_protocol.HanaExecutionRequest, directory: Path, arguments: list[str]) -> execution_identity.HanaCommandAdmissionRefused | None:
        return self._start(request, directory, arguments, 'source-update', hana_protocol.CONTROL_MAX_BYTES)

    def _start(self, request: hana_protocol.HanaExecutionRequest, directory: Path, arguments: list[str],
               action: str, output_max_bytes: int) -> execution_identity.HanaCommandAdmissionRefused | None:
        try:
            _ = hana_payload.read_copy_policy(self.copy_policy)
        except ValueError as error:
            return execution_identity.HanaCommandAdmissionRefused(str(error))
        try:
            payload = hana_protocol.encode_document({'action': action, 'request': hana_protocol.request_fields(request),
                                                     'output_max_bytes': output_max_bytes})
            _ = (directory / 'input').write_bytes(payload)
            limit = hana_protocol.CONTROL_MAX_BYTES if action == 'source-update' else COMMAND_OUTPUT_MAX_BYTES
            command = [*CAPTURE_COMMAND, str(limit + 1),
                       arguments[0], '--quiet', '--pipe', '--property=ExitType=main',
                       f'--property=EnvironmentFile={RUNNER_ENVIRONMENT}',
                       f'--property=RuntimeMaxSec={self.settings.command_timeout_s:g}',
                       f'--property=TemporaryFileSystem={hana_payload.payload_mount(guest.RUNNER_HOME)}:'
                       + f'mode=1777,nodev,nosuid,size={self.settings.source_max_bytes}',
                       f'--property=TimeoutStopSec={self.timeout_s}',
                       '--unit=' + command_unit(directory),
                       *(word for word in arguments[1:] if not word.startswith('--unit=') and word != '--collect'),
                       str(self.settings.source_max_bytes), str(self.settings.source_download_timeout_s),
                       str(self.settings.command_timeout_s)]
            with (directory / 'input').open('rb') as source, (directory / 'output').open('wb') as output, (
                    directory / 'error').open('wb') as error:
                child = subprocess.Popen(command, stdin=source, stdout=output, stderr=error, start_new_session=True)
        except (OSError, ValueError):
            return execution_identity.HanaCommandAdmissionRefused('The receiver could not start the Hana command unit')
        self.children[directory] = child
        try:
            start_time = int(process_stat.process_fields(child.pid)[19])
        except (FileNotFoundError, ProcessLookupError):
            # A fast failure can exit before the receiver records its process identity.
            _ = self._launcher_ended(directory)
        else:
            guest.atomic_record(directory / 'launcher.json', {'pid': child.pid, 'start_time': start_time})
        return None

    def _launcher_ended(self, directory: Path) -> bool:
        child = self.children.get(directory)
        if child is not None:
            status = child.poll()
            if status is not None and status >= 0 and not (directory / 'launcher-finished.json').exists():
                guest.atomic_record(directory / 'launcher-finished.json', {'status': status})
            return status is not None
        try:
            row = hana_result.object_fields(cast(object, json.loads((directory / 'launcher.json').read_text())))
            pid = hana_result.integer_field(row, 'pid')
            fields = process_stat.process_fields(pid)
            return fields[0] in ('Z', 'X') or int(fields[19]) != hana_result.integer_field(row, 'start_time')
        except (FileNotFoundError, ProcessLookupError):
            # A missing launch record cannot prove that publication did not start.
            return (directory / 'launcher.json').exists() or (directory / 'launcher-finished.json').exists()
        except (OSError, ValueError, KeyError):
            return False

    def _unit_exit_timing(self, directory: Path) -> hana_protocol.HanaCommandTiming:
        """Read the unit exit time even after systemd unloads the service."""
        try:
            response = subprocess.run(['journalctl', '--boot', '--no-pager', '--lines=1', '--output=json',
                '--output-fields=__REALTIME_TIMESTAMP', '_PID=1', 'UNIT=' + command_unit(directory),
                *('MESSAGE_ID=' + message for message in UNIT_EXIT_MESSAGES)], capture_output=True,
                timeout=self.timeout_s, check=True)
            timestamp = hana_result.string_field(read_document(response.stdout), '__REALTIME_TIMESTAMP')
            if timestamp.isascii() and timestamp.isdecimal():
                return {'unit_exited_at': int(timestamp) / 1_000_000}
        except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
            pass
        return {}

    def _record_unit_exit_timing(self, directory: Path,
                                 departure: hana_protocol.HanaCommandTiming) -> hana_protocol.HanaCommandTiming:
        path = directory / 'launcher-finished.json'
        if not path.exists():
            path = directory / 'launcher.json'
        try:
            record = read_document(path.read_bytes())
            saved = hana_protocol.decode_command_timing(record)
        except (OSError, ValueError):
            return self._unit_exit_timing(directory)
        if 'unit_exited_at' in saved:
            return {'unit_exited_at': saved['unit_exited_at']}
        if 'lease_free_at' in saved:
            return {}
        timing = self._unit_exit_timing(directory)
        update: hana_protocol.HanaCommandTiming = {**timing}
        if 'lease_free_at' in departure:
            update['lease_free_at'] = departure['lease_free_at']
        if update:
            try:
                guest.atomic_record(path, {**record, **update})
            except OSError:
                # A timing write does not affect departure proof.
                pass
        return timing

    def stop(self, directory: Path) -> None:
        """Keep an uncertain launcher owned until its unit confirms departure."""
        try:
            result = subprocess.run(['systemctl', 'stop', command_unit(directory)], capture_output=True,
                                    timeout=self.timeout_s, check=False)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError('The Hana command stop timed out') from error
        if result.returncode == 0:
            guest.atomic_record(directory / 'stopped.json', {'stopped': True})

    def departure(self, directory: Path, copy_number: int) -> CommandDeparture:
        try:
            response = subprocess.run(['systemctl', 'show', command_unit(directory),
                '--property=LoadState,ActiveState,ControlGroup,Job,Result'], capture_output=True,
                text=True, timeout=self.timeout_s, check=False)
            fields = dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)
            group = fields.get('ControlGroup', '')
            group_path = Path('/sys/fs/cgroup') / group.lstrip('/')
            empty_group = False
            if group and '..' not in Path(group).parts:
                try:
                    empty_group = 'populated 0' in (group_path / 'cgroup.events').read_text().splitlines()
                except FileNotFoundError:
                    empty_group = True
            if fields.get('ActiveState') == 'failed' and fields.get('LoadState') == 'loaded':
                if fields.get('Result') == 'timeout':
                    guest.atomic_record(directory / 'timed-out.json', {'timed_out': True})
                if not group:
                    empty_group = True
            launcher_ended = self._launcher_ended(directory)
            if fields.get('LoadState') == 'not-found':
                empty_group = ((directory / 'launcher-finished.json').exists()
                               or (directory / 'stopped.json').exists())
            if fields.get('ActiveState') not in ('inactive', 'failed') or fields.get('Job', '') not in ('', '0'):
                return CommandDeparture(False, False)
            timing: hana_protocol.HanaCommandTiming = {}
            if empty_group:
                timing['unit_exit_observed_at'] = self.clock()
            terminated = empty_group and launcher_ended
            lease_free = False
            if terminated:
                policy = hana_payload.read_copy_policy(self.copy_policy)
                probe_timing = build_folder_lock.ProbeTiming(policy.probe_wait_seconds, policy.probe_retry_seconds)
                lease_free = build_folder_lock.probe(
                    policy.copy_path(copy_number), policy.build_root, lambda: probe_timing) == 'free'
                if lease_free:
                    timing['lease_free_at'] = self.clock()
            if launcher_ended:
                timing.update(self._record_unit_exit_timing(directory, timing))
            return CommandDeparture(terminated, lease_free, timing)
        except (OSError, ValueError, subprocess.SubprocessError):
            return CommandDeparture(False, False)

    def outcome(self, directory: Path) -> hana_protocol.HanaRunOutcome:
        if (directory / 'timed-out.json').exists():
            try:
                with (directory / 'output').open('rb') as output:
                    if output.read(1) == b'\n':
                        return hana_protocol.HanaExecutionLost('The Hana command exceeded its execution deadline')
            except OSError:
                pass
            return hana_protocol.HanaExecutionRefused('Hana could not start the build before the execution deadline')
        started = False
        try:
            with (directory / 'output').open('rb') as output:
                # The prepared shell writes the marker immediately before exec.
                started = output.read(1) == b'\n'
                if not started:
                    _ = output.seek(0)
                content = output.read(COMMAND_OUTPUT_MAX_BYTES + 1 - int(started))
            if len(content) + int(started) > COMMAND_OUTPUT_MAX_BYTES:
                reason = 'The Hana runner result exceeds its transport bound'
            else:
                outcome = hana_protocol.decode_outcome(content)
                if isinstance(outcome, hana_protocol.HanaCommandFinished):
                    if started:
                        return outcome
                    reason = 'The Hana runner returned a result without starting the build'
                else:
                    reason = outcome.reason
        except (OSError, ValueError, KeyError):
            reason = ('The Hana runner has no complete result' if started
                      else 'The Hana command unit exited before it could start the build')
        return (hana_protocol.HanaExecutionLost(reason) if started else hana_protocol.HanaExecutionRefused(reason))

    def source_outcome(self, directory: Path) -> hana_protocol.HanaSourceUpdateOutcome:
        if (directory / 'timed-out.json').exists():
            return hana_protocol.HanaSourceUpdateRefused('The Hana source repair exceeded its execution deadline')
        try:
            with (directory / 'output').open('rb') as output:
                content = output.read(hana_protocol.CONTROL_MAX_BYTES + 1)
            if len(content) > hana_protocol.CONTROL_MAX_BYTES:
                return hana_protocol.HanaSourceUpdateRefused('The source repair result exceeds its protocol bound')
            return hana_protocol.decode_source_outcome(content)
        except (OSError, ValueError, KeyError):
            return hana_protocol.HanaSourceUpdateRefused('The Hana source repair has no complete result')


def receive_source_update(settings: guest.SafetySettings, envelope: hana_protocol.HanaSourceUpdateEnvelope,
        record: dict[str, object], directory: Path, supervisor: CommandSupervisor, *,
        paths: guest.GuestPaths, clock: Callable[[], float], uptime: Callable[[], float]) -> None:
    """Prepare source once under the original attempt without command authority."""
    if record['accepted'] is not True or record['fenced'] is True or record['refused'] is True:
        raise ValueError('The source update requires an unfenced accepted attempt')
    original = hana_protocol.decode_request(json.dumps(record.get('request')).encode())
    expected = replace(original.manifest, bundle_sha256=envelope.manifest.bundle_sha256,
                       bundle_url=envelope.manifest.bundle_url, basis_commits=())
    if envelope.manifest != expected:
        raise ValueError('The source update changes the captured source or command binding')
    if record.get('source_update_started') is True:
        return
    outcome = supervisor.outcome(directory)
    if not isinstance(outcome, hana_protocol.HanaExecutionRefused) or not outcome.reason.startswith('MissingBasis: '):
        raise ValueError('The source update requires a MissingBasis outcome')
    departure = supervisor.departure(directory, envelope.copy_number)
    if not (departure.terminated and departure.lease_free):
        raise ValueError('The original unit must release its exact copy before source preparation')
    permission = hana_result.object_fields(record.get('permission'))
    boot = hana_result.string_field(permission, 'boot_id')
    generation = hana_result.string_field(permission, 'generation')
    updated = replace(original, manifest=envelope.manifest)
    update_directory = source_update_directory(directory)
    record['source_update_started'] = True
    guest.atomic_record(directory / 'command.json', record)
    launch_attempted = False

    def start(arguments: list[str]) -> execution_identity.HanaCommandAdmissionRefused | None:
        nonlocal launch_attempted
        current = guest.read_permit(paths.permit)
        if (not isinstance(current, guest.WorkPermit) or current.boot_id != boot
                or not generation.isdecimal() or not current.generation.isdecimal()
                or int(current.generation) < int(generation)):
            raise ValueError('The original execution permission is no longer current')
        try:
            update_directory.mkdir(mode=0o700, exist_ok=True)
        except OSError as error:
            return execution_identity.HanaCommandAdmissionRefused(
                f'The Hana source repair directory could not be created: {error.strerror or error}')
        launch_attempted = True
        return supervisor.start_source_update(updated, update_directory, arguments)

    try:
        refusal = guest.admit_command(settings, BOOTSTRAP_COMMAND, guest.DATA,
            mounted=supervisor.mounted(), start=start, paths=paths, clock=clock, uptime=uptime)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if launch_attempted:
            return
        refusal = execution_identity.HanaCommandAdmissionRefused(str(error))
    if isinstance(refusal, execution_identity.HanaCommandAdmissionRefused):
        record['source_update_launch_refused'] = True
        record['source_update_reason'] = refusal.reason
        guest.atomic_record(directory / 'command.json', record)


def receive_execution(settings: guest.SafetySettings, descriptor: dict[str, object],
                      envelope: hana_protocol.HanaExecutionEnvelope, supervisor: CommandSupervisor, *,
                      paths: guest.GuestPaths = guest.GUEST_PATHS,
                      clock: Callable[[], float] = time.time,
                      uptime: Callable[[], float] = time.monotonic) -> hana_protocol.HanaExecutionObservation:
    """Accept once or fence the exact attempt under one durable command lock."""
    key = envelope.request.key if isinstance(envelope, hana_protocol.HanaCommandEnvelope) else envelope.key
    copy_number = envelope.request.copy_number if isinstance(envelope, hana_protocol.HanaCommandEnvelope) else envelope.copy_number
    if (key.generation.deployment != descriptor.get('deployment')
            or key.generation.start_token != descriptor.get('start_token')
            or key.generation.boot_id != guest.read_identity(paths.boot_id)
            or envelope.access.expires_at <= clock()):
        raise ValueError('The Hana command identity or publication access expired')
    directory = command_directory(paths, key)
    with command_lock(paths):
        directory.mkdir(mode=0o700, exist_ok=True)
        record_path = directory / 'command.json'
        record: dict[str, object]
        try:
            record = hana_result.object_fields(cast(object, json.loads(record_path.read_text())))
            if record.get('copy_number') != copy_number or hana_result.decode_attempt_key(record.get('key')) != key:
                raise ValueError('The saved command identity differs')
            if (any(type(record.get(name)) is not bool for name in ('accepted', 'fenced', 'refused'))
                    or not (record['accepted'] or record['fenced'])):
                raise ValueError('The saved command state is invalid')
            if any(name in record and type(record[name]) is not bool for name in
                   ('source_update_started', 'source_updated', 'source_update_launch_refused')):
                raise ValueError('The saved source update state is invalid')
        except FileNotFoundError:
            record = {'key': hana_result.encode_attempt_key(key), 'copy_number': copy_number,
                      'accepted': False, 'fenced': False, 'refused': False,
                      'source_update_started': False, 'source_updated': False}
        if isinstance(envelope, hana_protocol.HanaFenceEnvelope):
            record['fenced'] = True
            guest.atomic_record(record_path, record)
            if record['accepted'] is True and record['refused'] is not True:
                supervisor.stop(directory)
            if record.get('source_update_started') is True and record.get('source_update_launch_refused') is not True:
                supervisor.stop(source_update_directory(directory))
        elif isinstance(envelope, hana_protocol.HanaSourceUpdateEnvelope):
            receive_source_update(settings, envelope, record, directory, supervisor,
                                  paths=paths, clock=clock, uptime=uptime)
        elif record['accepted'] is not True and record['fenced'] is not True:
            record['accepted'] = True
            record['request'] = cast(object, json.loads(hana_protocol.encode_request(envelope.request)))
            record['permission'] = asdict(envelope.permission)
            guest.atomic_record(record_path, record)
            launch_attempted = False

            def start(arguments: list[str]) -> execution_identity.HanaCommandAdmissionRefused | None:
                nonlocal launch_attempted
                if guest.read_permit(paths.permit) != envelope.permission:
                    raise ValueError('The acknowledged command permit changed')
                abi = envelope.request.manifest.runtime_abi
                if abi != hana_protocol.RUNTIME_ABI:
                    raise guest.GuestRuntimeAbiMismatch(abi, hana_protocol.RUNTIME_ABI)
                launch_attempted = True
                return supervisor.start(envelope, directory, arguments)

            code = guest.CommandAdmissionRefusalCode.PRE_COMMAND_FAILED
            try:
                refusal = guest.admit_command(settings, BOOTSTRAP_COMMAND, guest.DATA,
                    mounted=supervisor.mounted(), start=start, paths=paths, clock=clock, uptime=uptime)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                refusal = None if launch_attempted else execution_identity.HanaCommandAdmissionRefused(str(error))
                if isinstance(error, (guest.GuestAdmissionDisabled, guest.GuestDataNotMounted,
                                      guest.GuestPermitRefused, guest.GuestRuntimeAbiMismatch)):
                    code = error.code
            if isinstance(refusal, execution_identity.HanaCommandAdmissionRefused):
                record['refused'] = True
                record['reason'] = refusal.reason
                record['refusal_code'] = code.value
                guest.atomic_record(record_path, record)
        accepted, fenced = record['accepted'] is True, record['fenced'] is True
        departure = (CommandDeparture(True, True) if not accepted or record['refused'] is True
                     else supervisor.departure(directory, copy_number))
        saved_timing = hana_protocol.decode_command_timing(record.get('timing', {}))
        timing: hana_protocol.HanaCommandTiming = {**departure.timing, **saved_timing}
        if timing != saved_timing:
            record['timing'] = timing
            guest.atomic_record(record_path, record)
        reason = hana_protocol.COMMAND_RESULT_PENDING
        if record.get('source_update_started') is True:
            update_directory = source_update_directory(directory)
            launch_refused = record.get('source_update_launch_refused') is True
            source_departure = (CommandDeparture(True, True) if launch_refused
                                else supervisor.departure(update_directory, copy_number))
            departure = CommandDeparture(departure.terminated and source_departure.terminated,
                                         departure.lease_free and source_departure.lease_free)
            if departure.terminated and departure.lease_free:
                source_outcome = (hana_protocol.HanaSourceUpdateRefused(
                    hana_result.string_field(record, 'source_update_reason')) if launch_refused
                    else supervisor.source_outcome(update_directory))
                original = hana_protocol.decode_request(json.dumps(record.get('request')).encode())
                identity = hana_protocol.SourceIdentity(original.manifest.repository,
                    original.manifest.commit, original.manifest.toolchain)
                if isinstance(source_outcome, hana_protocol.HanaSourceUpdated) and source_outcome.identity == identity:
                    if not fenced and record.get('source_updated') is not True:
                        record['source_updated'] = True
                        guest.atomic_record(record_path, record)
                else:
                    reason = (source_outcome.reason if isinstance(source_outcome, hana_protocol.HanaSourceUpdateRefused)
                              else 'The Hana source repair returned a different source identity')
                    return confirmed_refusal(key, copy_number, fenced, clock(), reason, timing=timing)
        elif accepted and departure.terminated and departure.lease_free:
            if record['refused'] is True:
                reason = hana_result.string_field(record, 'reason')
                code = guest.CommandAdmissionRefusalCode(hana_result.string_field(record, 'refusal_code'))
                return confirmed_refusal(key, copy_number, fenced, clock(), reason, code, timing=timing)
            outcome = supervisor.outcome(directory)
            if isinstance(outcome, hana_protocol.HanaExecutionRefused):
                if not outcome.reason.startswith('MissingBasis: '):
                    return confirmed_refusal(key, copy_number, fenced, clock(), outcome.reason, timing=timing)
                if not fenced:
                    reason = outcome.reason
            elif isinstance(outcome, hana_protocol.HanaExecutionLost) and not fenced:
                reason = outcome.reason
        observed_at = clock()
        evidence = (execution_identity.ExecutionEvidenceUnavailable(reason, observed_at) if accepted
                    else execution_identity.ExecutionNotStarted())
        return hana_protocol.HanaCommandObservation(key, copy_number, accepted, fenced, departure.terminated,
                                departure.lease_free, observed_at, evidence, record.get('source_updated') is True,
                                timing)


def confirmed_refusal(key: hana_result.HanaAttemptKey, copy_number: int, fenced: bool, observed_at: float, reason: str,
                      code: guest.CommandAdmissionRefusalCode = guest.CommandAdmissionRefusalCode.PRE_COMMAND_FAILED, *,
                      timing: hana_protocol.HanaCommandTiming
                      ) -> hana_protocol.ConfirmedCommandAdmissionRefusal:
    return hana_protocol.ConfirmedCommandAdmissionRefusal(key, copy_number, fenced, observed_at,
        ' '.join(reason.splitlines())[:1024] or 'Hana could not start the build', code, timing)


@dataclass(frozen=True)
class ReceiverSettings:
    aws_timeout_s: int
    control_poll_seconds: int
    result_transfer_timeout_s: int
    source_max_bytes: int
    source_download_timeout_s: int
    command_timeout_s: float

    def __post_init__(self) -> None:
        if (min(self.aws_timeout_s, self.control_poll_seconds, self.result_transfer_timeout_s,
                self.source_max_bytes, self.source_download_timeout_s, self.command_timeout_s) <= 0
                or not math.isfinite(self.command_timeout_s)):
            raise ValueError('Hana receiver limits must be positive and finite')


def read_settings(configuration: conf.ConfigRead, *, command_timeout_s: float) -> ReceiverSettings:
    from scripts.lib import conf

    return ReceiverSettings(
        configuration.value('aws_timeout_s', conf.positive_whole, recover_invalid=False),
        configuration.value('control_poll_seconds', conf.positive_whole, recover_invalid=False),
        configuration.value('result_transfer_timeout_s', conf.positive_whole, recover_invalid=False),
        configuration.value('source_max_bytes', conf.positive_whole, recover_invalid=False),
        configuration.value('source_download_timeout_s', conf.positive_whole, recover_invalid=False),
        command_timeout_s)


def read_document(content: bytes) -> dict[str, object]:
    if len(content) > hana_protocol.CONTROL_MAX_BYTES:
        raise ValueError('The Hana control document exceeds its protocol limit')
    value = cast(object, json.loads(content))
    if not isinstance(value, dict):
        raise ValueError('The Hana control document must be an object')
    return cast(dict[str, object], value)


def signed_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError('The Hana control URL is missing')
    parts = urlsplit(value)
    if (parts.scheme != 'https' or not parts.hostname or not parts.hostname.endswith('.amazonaws.com')
            or '.s3.' not in parts.hostname or parts.username or parts.password or parts.fragment):
        raise ValueError('The Hana control URL is invalid')
    return value


def receive(settings: guest.SafetySettings, descriptor: dict[str, object], control: dict[str, object],
            *, paths: guest.GuestPaths = guest.GUEST_PATHS, clock: Callable[[], float] = time.time) -> dict[str, object]:
    """Verify a control message before acknowledging its installed permit."""
    if (descriptor.get('version') != 1 or not isinstance(descriptor.get('expires'), int)
            or clock() >= cast(int, descriptor['expires']) or not descriptor.get('start_token') or not descriptor.get('deployment')):
        raise ValueError('The Hana boot descriptor is invalid or expired')
    boot = guest.read_identity(paths.boot_id)
    if not boot:
        raise ValueError('The Hana boot identity is missing')
    reply: dict[str, object] = {'deployment': descriptor['deployment'], 'start_token': descriptor['start_token'],
        'boot_id': boot, 'at': int(clock())}
    if 'permit' not in control:
        return reply
    if control.get('deployment') != descriptor['deployment'] or control.get('start_token') != descriptor['start_token']:
        raise ValueError('The Hana control message belongs to another start')
    raw = control['permit']
    if not isinstance(raw, dict):
        raise ValueError('The Hana permit is invalid')
    permit_fields = cast(dict[str, object], raw)
    generation = permit_fields.get('generation')
    if (not isinstance(generation, str) or permit_fields.get('boot_id') != boot
            or not isinstance(control.get('token'), str) or not isinstance(control.get('execution'), str)):
        raise ValueError('The Hana permit identity is invalid')
    expected = control.get('expected_generation')
    if type(expected) is not int:
        raise ValueError('The Hana permit generation changed')
    permit = guest.WorkPermit(hana_result.seconds_field(permit_fields, 'expires_at'), generation, boot)
    guest.write_permit(settings, permit, expected_generation=expected, paths=paths, clock=clock)
    reply.update(token=control['token'], generation=int(generation), execution=control['execution'])
    return reply


def transfer(request: Request, timeout: int) -> bytes:
    with cast(HTTPResponse, urlopen(request, timeout=timeout)) as response:
        return response.read(hana_protocol.CONTROL_MAX_BYTES + 1)


@dataclass(frozen=True)
class ResultUploadNotStarted:
    """The receiver has made no upload attempt."""


@dataclass(frozen=True)
class ResultUploaded:
    """The result PUT completed."""


@dataclass(frozen=True)
class ResultUploadLost:
    """The receiver cannot confirm a result PUT."""
    reason: str


ResultUploadOutcome = ResultUploaded | ResultUploadLost


@dataclass(frozen=True)
class ResultUploadRunning:
    """The receiver must wait for curl before it publishes the result observation."""


def _stop_upload(process: subprocess.Popen[bytes]) -> None:
    process.kill()
    _ = process.wait()


def _upload_outcome(status: int, timeout: int) -> ResultUploadOutcome:
    if status == 0:
        return ResultUploaded()
    if status == 28:
        return ResultUploadLost(f'{hana_result.RESULT_UPLOAD_TIMEOUT}: The result upload exceeded '
                                + f'{timeout} s (result_transfer_timeout_s)')
    return _upload_loss(f'curl exited with status {status}')


def _upload_loss(reason: str) -> ResultUploadLost:
    return ResultUploadLost(f'{hana_result.RESULT_UPLOAD_FAILED}: {reason}')


def _upload_window_loss(timeout: int) -> ResultUploadLost:
    return _upload_loss(f'The signed access window cannot fit a result upload of {timeout} s and settlement')


def _read_upload_timing(path: Path) -> hana_protocol.HanaCommandTiming:
    try:
        return hana_protocol.decode_command_timing(read_document(path.read_bytes()))
    except (OSError, ValueError):
        return {}


def _recover_result_upload(path: Path) -> ResultUploadNotStarted | ResultUploadOutcome:
    try:
        row = read_document(path.read_bytes())
    except FileNotFoundError:
        return ResultUploadNotStarted()
    if row.get('state') == 'uploaded':
        return ResultUploaded()
    if row.get('state') == 'lost':
        return ResultUploadLost(hana_result.string_field(row, 'reason'))
    if row.get('state') == 'started':
        lost = _upload_loss('The receiver restarted before it confirmed the result upload')
        _save_result_upload(path, lost, _read_upload_timing(path))
        return lost
    raise ValueError('The Hana result upload record is invalid')


def _save_result_upload(path: Path, outcome: ResultUploadOutcome, timing: hana_protocol.HanaCommandTiming) -> None:
    row: dict[str, object] = ({'state': 'lost', 'reason': outcome.reason}
                              if isinstance(outcome, ResultUploadLost) else {'state': 'uploaded'})
    row.update(timing)
    guest.atomic_record(path, row)


def _result_upload(directory: Path, key: hana_result.HanaAttemptKey, access: hana_protocol.HanaExecutionAccess,
                   boot_deadline: int, transport: ReceiverSettings, supervisor: CommandSupervisor,
                   uploads: dict[hana_result.HanaAttemptKey, subprocess.Popen[bytes]],
                   clock: Callable[[], float]) -> ResultUploadNotStarted | ResultUploadOutcome | ResultUploadRunning:
    if key in uploads:
        return ResultUploadRunning()
    path = directory / 'result-upload.json'
    saved = _recover_result_upload(path)
    if not isinstance(saved, ResultUploadNotStarted):
        return saved
    result = supervisor.outcome(directory)
    if not isinstance(result, hana_protocol.HanaCommandFinished):
        return saved
    timeout = transport.result_transfer_timeout_s
    reserve = hana_protocol.fence_settlement_seconds(transport.aws_timeout_s, transport.control_poll_seconds)
    latest_start = min(boot_deadline, access.expires_at) - reserve - timeout
    if clock() < latest_start:
        url = signed_url(access.result_url)
        try:
            with tempfile.TemporaryFile() as payload, tempfile.TemporaryFile() as config:
                _ = payload.write(hana_result.encode_result(result.result))
                _ = payload.seek(0)
                _ = config.write(f'url = {json.dumps(url)}\n'.encode())
                _ = config.seek(0)
                started_at = clock()
                if started_at < latest_start:
                    fd = payload.fileno()
                    guest.atomic_record(path, {'state': 'started', 'upload_started_at': started_at})
                    if clock() < latest_start:
                        uploads[key] = subprocess.Popen(['curl', '--disable', '--silent', '--fail', '--proto', '=https',
                            '--max-time', str(timeout), '--upload-file', f'/proc/self/fd/{fd}', '--config', '-'],
                            stdin=config, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(fd,))
                        return ResultUploadRunning()
        except OSError:
            lost = _upload_loss('The receiver could not start the result upload')
            _save_result_upload(path, lost, {})
            return lost
    refused = _upload_window_loss(timeout)
    _save_result_upload(path, refused, {})
    return refused


@dataclass(frozen=True)
class ControlStepFailed:
    """A control step failed and the receiver can continue the pass."""
    cause: OSError | HTTPException | ValueError


@dataclass(frozen=True)
class ControlStepCompleted:
    """The receiver completed the control step."""


def _control_step(step: Callable[[], None]) -> ControlStepCompleted | ControlStepFailed:
    try:
        step()
    except (OSError, HTTPException, ValueError) as error:
        return ControlStepFailed(error)
    return ControlStepCompleted()


def run_receiver(settings: guest.SafetySettings, transport: ReceiverSettings,
                 *, paths: guest.GuestPaths = guest.GUEST_PATHS,
                 copy_policy: Path = hana_payload.IMAGE_COPY_POLICY,
                 fetch: Callable[[Request, int], bytes] = transfer, pause: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, keep_running: Callable[[], bool] = lambda: True) -> None:
    """Read root-only IMDS user data once per boot and publish scoped acknowledgements."""
    metadata = 'http://169.254.169.254/latest/'
    token = fetch(Request(metadata + 'api/token', method='PUT',
        headers={'X-aws-ec2-metadata-token-ttl-seconds': '21600'}), transport.aws_timeout_s).decode()
    descriptor = read_document(fetch(Request(metadata + 'user-data',
        headers={'X-aws-ec2-metadata-token': token}), transport.aws_timeout_s))
    if descriptor.get('setup_only') is True:
        return
    boot_deadline = hana_result.integer_field(descriptor, 'expires')
    timeout = transport.aws_timeout_s
    supervisor = GuestCommandSupervisor(transport, copy_policy=copy_policy, clock=clock)
    uploads: dict[hana_result.HanaAttemptKey, subprocess.Popen[bytes]] = {}

    def process_permit() -> None:
        if clock() >= boot_deadline:
            return
        url = signed_url(descriptor.get('control_url'))
        control = read_document(fetch(Request(url), timeout))
        if clock() < boot_deadline:
            reply = receive(settings, descriptor, control, paths=paths, clock=clock)
            report_url = signed_url(descriptor.get('report_url'))
            _ = fetch(Request(report_url, data=json.dumps(reply).encode(), method='PUT'), timeout)

    def process_execution(name: str) -> None:
        if name not in descriptor or clock() >= boot_deadline:
            return
        content = fetch(Request(signed_url(descriptor[name])), timeout)
        document = read_document(content)
        if document.get('version') == 1 and document.get('action') == 'idle':
            return
        envelope = hana_protocol.decode_envelope(content)
        if clock() >= min(boot_deadline, envelope.access.expires_at):
            return
        observation = receive_execution(settings, descriptor, envelope, supervisor, paths=paths, clock=clock)
        directory = command_directory(paths, observation.key)
        if observation.fenced:
            process = uploads.get(observation.key)
            if process is not None:
                _stop_upload(process)
                del uploads[observation.key]
        elif (isinstance(observation, hana_protocol.HanaCommandObservation)
              and observation.accepted and observation.terminated and observation.lease_free):
            result = _result_upload(directory, observation.key, envelope.access, boot_deadline,
                                    transport, supervisor, uploads, clock)
            if isinstance(result, ResultUploadRunning):
                return
            if isinstance(result, ResultUploadLost):
                observation = replace(observation,
                    evidence=execution_identity.ExecutionEvidenceUnavailable(result.reason, observation.observed_at))
        if observation.fenced or (observation.terminated and observation.lease_free):
            upload_timing = _read_upload_timing(directory / 'result-upload.json')
            if upload_timing:
                timing: hana_protocol.HanaCommandTiming = {**observation.timing, **upload_timing}
                observation = replace(observation, timing=timing)
        url = signed_url(envelope.access.observation_url)
        _ = fetch(Request(url, data=hana_protocol.encode_observation(observation), method='PUT'), timeout)

    def poll_upload(key: hana_result.HanaAttemptKey, process: subprocess.Popen[bytes]) -> None:
        status = process.poll()
        if status is None:
            return
        path = command_directory(paths, key) / 'result-upload.json'
        _save_result_upload(path, _upload_outcome(status, transport.result_transfer_timeout_s), _read_upload_timing(path))
        del uploads[key]

    try:
        while keep_running() and clock() < boot_deadline:
            _ = _control_step(lambda: process_execution('fence_url'))
            _ = _control_step(process_permit)
            for key, process in tuple(uploads.items()):
                _ = _control_step(lambda: poll_upload(key, process))
            _ = _control_step(lambda: process_execution('command_url'))
            pause(transport.control_poll_seconds)
    finally:
        for process in uploads.values():
            _stop_upload(process)


def receiver_program() -> str:
    """Load the reviewed runtime while keeping safety services independent."""
    return f'''import sys
sys.path.insert(0, {str(guest.IMAGE_RUNTIME)!r})
from scripts.aws_target.hana_receiver import installed_main
if __name__ == '__main__':
    installed_main()
'''


def installed_main() -> None:
    try:
        values = hana_result.object_fields(cast(object, json.loads((guest.CONTROL / 'receiver.json').read_text())))
        safety = guest.decode_settings(hana_result.object_fields(cast(object,
            json.loads((guest.CONTROL / 'safety.json').read_text()))))
        run_receiver(safety, ReceiverSettings(hana_result.integer_field(values, 'aws_timeout_s'),
                                            hana_result.integer_field(values, 'control_poll_seconds'),
                                            hana_result.integer_field(values, 'result_transfer_timeout_s'),
                                            hana_result.integer_field(values, 'source_max_bytes'),
                                            hana_result.integer_field(values, 'source_download_timeout_s'),
                                            hana_result.seconds_field(values, 'command_timeout_s')))
    except Exception:
        # The independent guard stops the box after a receiver failure.
        raise SystemExit('Hana receiver refused the control message') from None


def render_files(settings: ReceiverSettings) -> tuple[guest.GuestFile, ...]:
    unit = f'''[Unit]
Description=Hana root control receiver
After=network-online.target
Wants=network-online.target
Before=hana-tools.service

[Service]
Type=simple
User=root
UMask=0077
Environment=PATH={guest.SYSTEM_BIN}
ExecStart={guest.SYSTEM_BIN}/python3 -I {guest.CONTROL}/receiver.py
Restart=on-failure
RestartSec={settings.control_poll_seconds}

[Install]
WantedBy=multi-user.target
'''
    return (guest.GuestFile(guest.CONTROL / 'receiver.py', receiver_program(), 0o600),
        guest.GuestFile(guest.CONTROL / 'receiver.json', json.dumps(asdict(settings)) + '\n', 0o600),
        guest.GuestFile(Path('/etc/systemd/system/hana-receiver.service'), unit, 0o644))

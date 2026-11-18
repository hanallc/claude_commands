"""Receive signed control objects as root without guest AWS credentials."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import fcntl
import hashlib
from http.client import HTTPException, HTTPResponse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Protocol, cast
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from scripts.aws_target import hana_guest as guest
from scripts.aws_target import hana_execution as execution, hana_result, hana_runner, hana_source
from scripts.build_queue import placement
from scripts.lib import build_folders, build_slots, conf

# Control messages have a fixed protocol size limit.
CONTROL_MAX_BYTES = 65536
RUNTIME = Path('/var/lib/hana-runtime')
MOUNTINFO = Path('/proc/self/mountinfo')


@dataclass(frozen=True)
class CommandDeparture:
    terminated: bool
    lease_free: bool


class CommandSupervisor(Protocol):
    def mounted(self) -> bool: ...
    def start(self, envelope: execution.HanaCommandEnvelope, directory: Path, arguments: list[str]) -> None: ...
    def stop(self, directory: Path) -> None: ...
    def departure(self, directory: Path, slot: int) -> CommandDeparture: ...
    def outcome(self, directory: Path) -> hana_runner.HanaRunOutcome: ...
    def start_source_update(self, request: execution.HanaExecutionRequest, directory: Path, arguments: list[str]) -> None: ...
    def source_outcome(self, directory: Path) -> hana_runner.HanaSourceUpdateOutcome: ...


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

    def __init__(self, timeout_s: int) -> None:
        self.timeout_s: int = timeout_s
        self.children: dict[Path, subprocess.Popen[bytes]] = {}

    def mounted(self) -> bool:
        return data_mount_ready()

    def start(self, envelope: execution.HanaCommandEnvelope, directory: Path, arguments: list[str]) -> None:
        self._start(envelope.request, directory, arguments, 'execute', envelope.access.output_max_bytes)

    def start_source_update(self, request: execution.HanaExecutionRequest, directory: Path, arguments: list[str]) -> None:
        self._start(request, directory, arguments, 'source-update', CONTROL_MAX_BYTES)

    def _start(self, request: execution.HanaExecutionRequest, directory: Path, arguments: list[str],
               action: str, output_max_bytes: int) -> None:
        payload = json.dumps({'action': action, 'request': cast(object, json.loads(execution.encode_request(request))),
                             'output_max_bytes': output_max_bytes}).encode()
        _ = (directory / 'input').write_bytes(payload)
        timeouts = hana_runner.read_timeouts()
        with (directory / 'input').open('rb') as source, (directory / 'output').open('wb') as output, (
                directory / 'error').open('wb') as error:
            command = [arguments[0], '--quiet', '--pipe', '--property=ExitType=cgroup',
                       '--property=EnvironmentFile=/etc/hana-aws/runner-environment',
                       f'--property=RuntimeMaxSec={timeouts.command_timeout_s:g}',
                       f'--property=TimeoutStopSec={self.timeout_s}',
                       '--unit=' + command_unit(directory),
                       *(word for word in arguments[1:] if not word.startswith('--unit=') and word != '--collect')]
            child = subprocess.Popen(command, stdin=source, stdout=output, stderr=error, start_new_session=True)
        self.children[directory] = child
        from scripts.mac_test.linux_runner import process_fields

        guest.atomic_record(directory / 'launcher.json', {'pid': child.pid,
            'start_time': int(process_fields(child.pid)[19])})

    def _launcher_ended(self, directory: Path) -> bool:
        child = self.children.get(directory)
        if child is not None:
            status = child.poll()
            if status == 0:
                guest.atomic_record(directory / 'launcher-finished.json', {'status': status})
            return status is not None
        from scripts.mac_test.linux_runner import process_fields

        try:
            row = hana_result.object_fields(cast(object, json.loads((directory / 'launcher.json').read_text())))
            pid = hana_result.integer_field(row, 'pid')
            fields = process_fields(pid)
            return fields[0] in ('Z', 'X') or int(fields[19]) != hana_result.integer_field(row, 'start_time')
        except (FileNotFoundError, ProcessLookupError):
            # A missing launch record cannot prove that publication did not start.
            return (directory / 'launcher.json').exists()
        except (OSError, ValueError, KeyError):
            return False

    def stop(self, directory: Path) -> None:
        """Keep an uncertain launcher owned until its unit confirms departure."""
        try:
            result = subprocess.run(['systemctl', 'stop', command_unit(directory)], capture_output=True,
                                    timeout=self.timeout_s, check=False)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError('The Hana command stop timed out') from error
        if result.returncode == 0:
            guest.atomic_record(directory / 'stopped.json', {'stopped': True})

    def departure(self, directory: Path, slot: int) -> CommandDeparture:
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
            if fields.get('Result') == 'timeout' and fields.get('ActiveState') == 'failed':
                guest.atomic_record(directory / 'timed-out.json', {'timed_out': True})
                if not group and fields.get('LoadState') == 'loaded':
                    empty_group = True
            launcher_ended = self._launcher_ended(directory)
            if fields.get('LoadState') == 'not-found':
                empty_group = ((directory / 'launcher-finished.json').exists()
                               or (directory / 'stopped.json').exists())
            ended = (launcher_ended and empty_group
                and fields.get('ActiveState') in ('inactive', 'failed') and fields.get('Job', '') in ('', '0'))
            root = build_slots.configured_slot_root()
            lease_free = build_folders.build_folder_state(root / f'hana-slot{slot}') == 'free'
            return CommandDeparture(ended, ended and lease_free)
        except (OSError, ValueError, subprocess.SubprocessError):
            return CommandDeparture(False, False)

    def outcome(self, directory: Path) -> hana_runner.HanaRunOutcome:
        if (directory / 'timed-out.json').exists():
            return hana_runner.HanaExecutionLost('The Hana command exceeded its execution deadline')
        try:
            with (directory / 'output').open('rb') as output:
                content = output.read(hana_result.RESULT_MAX_BYTES + 1)
            if len(content) > hana_result.RESULT_MAX_BYTES:
                return hana_runner.HanaExecutionLost('The runner result exceeds its transport bound')
            return hana_runner.decode_outcome(content)
        except (OSError, ValueError, KeyError):
            return hana_runner.HanaExecutionLost('The runner has no complete result')

    def source_outcome(self, directory: Path) -> hana_runner.HanaSourceUpdateOutcome:
        try:
            with (directory / 'output').open('rb') as output:
                content = output.read(CONTROL_MAX_BYTES + 1)
            if len(content) > CONTROL_MAX_BYTES:
                raise ValueError('The source update result exceeds its protocol bound')
            return hana_runner.decode_source_outcome(content)
        except (OSError, ValueError, KeyError) as error:
            return hana_runner.HanaSourceUpdateRefused(str(error))


def receive_source_update(settings: guest.SafetySettings, envelope: execution.HanaSourceUpdateEnvelope,
        record: dict[str, object], directory: Path, supervisor: CommandSupervisor, *,
        paths: guest.GuestPaths, clock: Callable[[], float], uptime: Callable[[], float]) -> None:
    """Prepare source once under the original attempt without command authority."""
    if record['accepted'] is not True or record['fenced'] is True or record['refused'] is True:
        raise ValueError('The source update requires an unfenced accepted attempt')
    original = execution.decode_request(json.dumps(record.get('request')).encode())
    expected = replace(original.manifest, bundle_sha256=envelope.manifest.bundle_sha256,
                       bundle_url=envelope.manifest.bundle_url, basis_commits=())
    if envelope.manifest != expected:
        raise ValueError('The source update changes the captured source or command binding')
    if record.get('source_update_started') is True:
        return
    outcome = supervisor.outcome(directory)
    if not isinstance(outcome, hana_runner.HanaExecutionRefused) or not outcome.reason.startswith('MissingBasis: '):
        raise ValueError('The source update requires a MissingBasis outcome')
    departure = supervisor.departure(directory, envelope.slot)
    if not (departure.terminated and departure.lease_free):
        raise ValueError('The original unit must release its exact slot before source preparation')
    permission = hana_result.object_fields(record.get('permission'))
    boot = hana_result.string_field(permission, 'boot_id')
    generation = hana_result.string_field(permission, 'generation')
    updated = replace(original, manifest=envelope.manifest)
    update_directory = source_update_directory(directory)
    update_directory.mkdir(mode=0o700, exist_ok=True)
    record['source_update_started'] = True
    guest.atomic_record(directory / 'command.json', record)
    launch_attempted = False

    def start(arguments: list[str]) -> None:
        nonlocal launch_attempted
        current = guest.read_permit(paths.permit)
        if (not isinstance(current, guest.WorkPermit) or current.boot_id != boot
                or not generation.isdecimal() or not current.generation.isdecimal()
                or int(current.generation) < int(generation)):
            raise ValueError('The original execution permission is no longer current')
        launch_attempted = True
        supervisor.start_source_update(updated, update_directory, arguments)

    try:
        guest.admit_command(settings, (guest.SYSTEM_BIN + '/python3', '-E', str(RUNTIME / 'runner.py')), guest.DATA,
            mounted=supervisor.mounted(), start=start, paths=paths, clock=clock, uptime=uptime)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        record['source_update_launch_refused'] = not launch_attempted
        record['reason'] = str(error)
        guest.atomic_record(directory / 'command.json', record)


def receive_execution(settings: guest.SafetySettings, descriptor: dict[str, object],
                      envelope: execution.HanaExecutionEnvelope, supervisor: CommandSupervisor, *,
                      paths: guest.GuestPaths = guest.GUEST_PATHS,
                      clock: Callable[[], float] = time.time,
                      uptime: Callable[[], float] = time.monotonic) -> execution.HanaCommandObservation:
    """Accept once or fence the exact attempt under one durable command lock."""
    key = envelope.request.key if isinstance(envelope, execution.HanaCommandEnvelope) else envelope.key
    slot = envelope.request.slot if isinstance(envelope, execution.HanaCommandEnvelope) else envelope.slot
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
            if record.get('slot') != slot or hana_result.decode_attempt_key(record.get('key')) != key:
                raise ValueError('The saved command identity differs')
            if (any(type(record.get(name)) is not bool for name in ('accepted', 'fenced', 'refused'))
                    or not (record['accepted'] or record['fenced'])):
                raise ValueError('The saved command state is invalid')
            if any(name in record and type(record[name]) is not bool for name in
                   ('source_update_started', 'source_updated', 'source_update_launch_refused')):
                raise ValueError('The saved source update state is invalid')
        except FileNotFoundError:
            record = {'key': hana_result.encode_attempt_key(key), 'slot': slot,
                      'accepted': False, 'fenced': False, 'refused': False,
                      'source_update_started': False, 'source_updated': False}
        if isinstance(envelope, execution.HanaFenceEnvelope):
            record['fenced'] = True
            guest.atomic_record(record_path, record)
            if record['accepted'] is True and record['refused'] is not True:
                supervisor.stop(directory)
            if record.get('source_update_started') is True and record.get('source_update_launch_refused') is not True:
                supervisor.stop(source_update_directory(directory))
        elif isinstance(envelope, execution.HanaSourceUpdateEnvelope):
            receive_source_update(settings, envelope, record, directory, supervisor,
                                  paths=paths, clock=clock, uptime=uptime)
        elif record['accepted'] is not True and record['fenced'] is not True:
            record['accepted'] = True
            record['request'] = cast(object, json.loads(execution.encode_request(envelope.request)))
            record['permission'] = asdict(envelope.permission)
            guest.atomic_record(record_path, record)
            launch_attempted = False

            def start(arguments: list[str]) -> None:
                nonlocal launch_attempted
                if guest.read_permit(paths.permit) != envelope.permission:
                    raise ValueError('The acknowledged command permit changed')
                launch_attempted = True
                supervisor.start(envelope, directory, arguments)

            try:
                guest.admit_command(settings, (guest.SYSTEM_BIN + '/python3', '-E',
                    str(RUNTIME / 'runner.py')), guest.DATA,
                    mounted=supervisor.mounted(), start=start, paths=paths, clock=clock, uptime=uptime)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                if not launch_attempted:
                    record['refused'] = True
                record['reason'] = str(error)
                guest.atomic_record(record_path, record)
        accepted, fenced = record['accepted'] is True, record['fenced'] is True
        departure = (CommandDeparture(True, True) if not accepted or record['refused'] is True
                     else supervisor.departure(directory, slot))
        if record.get('source_update_started') is True:
            update_directory = source_update_directory(directory)
            source_departure = (CommandDeparture(True, True) if record.get('source_update_launch_refused') is True
                                else supervisor.departure(update_directory, slot))
            departure = CommandDeparture(departure.terminated and source_departure.terminated,
                                         departure.lease_free and source_departure.lease_free)
            if departure.terminated and departure.lease_free and not fenced:
                source_outcome = supervisor.source_outcome(update_directory)
                original = execution.decode_request(json.dumps(record.get('request')).encode())
                identity = hana_source.SourceIdentity(original.manifest.repository,
                    original.manifest.commit, original.manifest.toolchain)
                if isinstance(source_outcome, hana_runner.HanaSourceUpdated) and source_outcome.identity == identity:
                    record['source_updated'] = True
                    guest.atomic_record(record_path, record)
        outcome = supervisor.outcome(directory) if accepted else hana_runner.HanaExecutionLost('The command never started')
        reason = str(record.get('reason', 'The command result is pending'))
        if isinstance(outcome, hana_runner.HanaExecutionRefused):
            reason = outcome.reason
        elif isinstance(outcome, hana_runner.HanaExecutionLost) and 'reason' not in record:
            reason = outcome.reason
        evidence = (placement.ExecutionEvidenceUnavailable(reason, clock()) if accepted
                    else placement.ExecutionNotStarted())
        return execution.HanaCommandObservation(key, slot, accepted, fenced, departure.terminated,
                                                departure.lease_free, clock(), evidence, record.get('source_updated') is True)


@dataclass(frozen=True)
class ReceiverSettings:
    aws_timeout_s: int
    control_poll_seconds: int
    result_transfer_timeout_s: int


def read_settings(configuration: conf.ConfigRead) -> ReceiverSettings:
    settings = ReceiverSettings(configuration.whole('aws_timeout_s'), configuration.whole('control_poll_seconds'),
                                configuration.whole('result_transfer_timeout_s'))
    if min(settings.aws_timeout_s, settings.control_poll_seconds, settings.result_transfer_timeout_s) <= 0:
        raise ValueError('Hana receiver intervals must be positive')
    return settings


def read_document(content: bytes) -> dict[str, object]:
    if len(content) > CONTROL_MAX_BYTES:
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
    if (not isinstance(generation, str) or not generation.isdecimal()
            or int(generation) <= 0 or permit_fields.get('boot_id') != boot
            or not isinstance(control.get('token'), str) or not isinstance(control.get('execution'), str)):
        raise ValueError('The Hana permit identity is invalid')
    permit = guest.WorkPermit(hana_result.seconds_field(permit_fields, 'expires_at'), generation, boot)
    with guest.guest_lock(paths):
        if paths.fence.exists():
            raise ValueError('Hana shutdown is fenced')
        previous = guest.read_identity(paths.generation)
        current = guest.read_permit(paths.permit)
        if not (isinstance(current, guest.WorkPermit) and current == permit):
            expected = control.get('expected_generation')
            if type(expected) is not int or expected != int(previous or '0'):
                raise ValueError('The Hana permit generation changed')
            if not guest.fresh_permit(settings, permit, generation, boot, clock):
                raise ValueError('The Hana permit is expired or exceeds its window')
            if previous and int(generation) <= int(previous):
                raise ValueError('The Hana permit generation did not increase')
            # A failed partial write refuses permission until a controller recovery.
            guest.atomic_record(paths.permit, asdict(permit))
            _ = paths.generation.write_text(generation)
        elif not guest.fresh_permit(settings, permit, generation, boot, clock):
            raise ValueError('The Hana permit expired')
    reply.update(token=control['token'], generation=int(generation), execution=control['execution'])
    return reply


def transfer(request: Request, timeout: int) -> bytes:
    with cast(HTTPResponse, urlopen(request, timeout=timeout)) as response:
        return response.read(CONTROL_MAX_BYTES + 1)


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
        _save_result_upload(path, lost)
        return lost
    raise ValueError('The Hana result upload record is invalid')


def _save_result_upload(path: Path, outcome: ResultUploadOutcome) -> None:
    row = {'state': 'lost', 'reason': outcome.reason} if isinstance(outcome, ResultUploadLost) else {'state': 'uploaded'}
    guest.atomic_record(path, row)


def _result_upload(directory: Path, key: hana_result.HanaAttemptKey, access: execution.HanaExecutionAccess,
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
    if not isinstance(result, hana_runner.HanaCommandFinished):
        return saved
    timeout = transport.result_transfer_timeout_s
    reserve = execution.fence_settlement_seconds(transport.aws_timeout_s, transport.control_poll_seconds)
    latest_start = min(boot_deadline, access.expires_at) - reserve - timeout
    if clock() < latest_start:
        url = signed_url(access.result_url)
        guest.atomic_record(path, {'state': 'started'})
        try:
            with tempfile.TemporaryFile() as payload, tempfile.TemporaryFile() as config:
                _ = payload.write(hana_result.encode_result(result.result))
                _ = payload.seek(0)
                _ = config.write(f'url = {json.dumps(url)}\n'.encode())
                _ = config.seek(0)
                if clock() < latest_start:
                    fd = payload.fileno()
                    uploads[key] = subprocess.Popen(['curl', '--disable', '--silent', '--fail', '--proto', '=https',
                        '--max-time', str(timeout), '--upload-file', f'/proc/self/fd/{fd}', '--config', '-'],
                        stdin=config, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(fd,))
                    return ResultUploadRunning()
        except OSError:
            lost = _upload_loss('The receiver could not start the result upload')
            _save_result_upload(path, lost)
            return lost
    refused = _upload_window_loss(timeout)
    _save_result_upload(path, refused)
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
    supervisor = GuestCommandSupervisor(timeout)
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
        envelope = execution.decode_envelope(content)
        if clock() >= min(boot_deadline, envelope.access.expires_at):
            return
        observation = receive_execution(settings, descriptor, envelope, supervisor, paths=paths, clock=clock)
        if observation.fenced:
            process = uploads.get(observation.key)
            if process is not None:
                _stop_upload(process)
                del uploads[observation.key]
        elif observation.accepted and observation.terminated and observation.lease_free:
            directory = command_directory(paths, observation.key)
            result = _result_upload(directory, observation.key, envelope.access, boot_deadline,
                                    transport, supervisor, uploads, clock)
            if isinstance(result, ResultUploadRunning):
                return
            if isinstance(result, ResultUploadLost):
                observation = replace(observation,
                    evidence=placement.ExecutionEvidenceUnavailable(result.reason, observation.observed_at))
        url = signed_url(envelope.access.observation_url)
        _ = fetch(Request(url, data=execution.encode_observation(observation), method='PUT'), timeout)

    def poll_upload(key: hana_result.HanaAttemptKey, process: subprocess.Popen[bytes]) -> None:
        status = process.poll()
        if status is None:
            return
        path = command_directory(paths, key) / 'result-upload.json'
        _save_result_upload(path, _upload_outcome(status, transport.result_transfer_timeout_s))
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
sys.path.insert(0, {str(RUNTIME)!r})
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
                                            hana_result.integer_field(values, 'result_transfer_timeout_s')))
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
Environment=CONF_DIR={RUNTIME}/config
ExecStart={guest.SYSTEM_BIN}/python3 {guest.CONTROL}/receiver.py
Restart=on-failure
RestartSec={settings.control_poll_seconds}

[Install]
WantedBy=multi-user.target
'''
    return (guest.GuestFile(guest.CONTROL / 'receiver.py', receiver_program(), 0o600),
        guest.GuestFile(guest.CONTROL / 'receiver.json', json.dumps(asdict(settings)) + '\n', 0o600),
        guest.GuestFile(Path('/etc/systemd/system/hana-receiver.service'), unit, 0o644))

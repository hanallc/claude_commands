"""Execute exact Hana source inside the unprivileged command unit."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
from typing import TYPE_CHECKING, cast

from scripts.aws_target import hana_source as source
from scripts.aws_target.hana_result import HanaCommandResult, PeakUnavailable, decode_result, encode_result
from scripts.lib import build_slots, conf
from scripts.mac_test import linux_runner

if TYPE_CHECKING:
    from scripts.aws_target.hana_execution import HanaExecutionRequest


IMAGE_COMPILER_VERSION = Path('/etc/hana-aws/compiler-version')


class HanaCompilerVersionUnavailable(ValueError):
    """The image has no usable shared compiler release."""


def read_image_compiler_version() -> str:
    try:
        version = IMAGE_COMPILER_VERSION.read_text().strip()
    except (OSError, UnicodeError) as error:
        raise HanaCompilerVersionUnavailable('HanaCompilerVersionUnavailable: The image compiler version is unavailable') from error
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise HanaCompilerVersionUnavailable('HanaCompilerVersionUnavailable: The image compiler version is invalid')
    return version


@dataclass(frozen=True)
class HanaCommandFinished:
    result: HanaCommandResult


@dataclass(frozen=True)
class HanaExecutionRefused:
    reason: str


@dataclass(frozen=True)
class HanaExecutionLost:
    reason: str


HanaRunOutcome = HanaCommandFinished | HanaExecutionRefused | HanaExecutionLost


@dataclass(frozen=True)
class HanaSourceUpdated:
    identity: source.SourceIdentity


@dataclass(frozen=True)
class HanaSourceUpdateRefused:
    reason: str


HanaSourceUpdateOutcome = HanaSourceUpdated | HanaSourceUpdateRefused


@dataclass(frozen=True)
class GuestExecutionSettings:
    shell: str
    shell_timeout_s: float
    compiler_version: str
    free_floor_gib: float
    command_timeout_s: float


@dataclass(frozen=True)
class GuestExecutionTimeouts:
    shell_timeout_s: float
    command_timeout_s: float


def read_timeouts() -> GuestExecutionTimeouts:
    values = conf.read_or_repository('build_targets').values()
    return GuestExecutionTimeouts(linux_runner.positive_setting(values, 'loopback.shell_timeout_s'),
        linux_runner.positive_setting(values, 'loopback.command_timeout_s'))


def read_settings(configuration: conf.ConfigRead) -> GuestExecutionSettings:
    values = configuration.values()
    timeouts = read_timeouts()
    settings = GuestExecutionSettings(configuration.text('execution_shell'),
        timeouts.shell_timeout_s,
        read_image_compiler_version(),
        linux_runner.positive_setting(values, 'free_floor_gib'),
        timeouts.command_timeout_s)
    if not settings.shell or not settings.compiler_version:
        raise ValueError('The guest requires a shell and a compiler version')
    return settings


def materialize_and_run(request: HanaExecutionRequest, *, root: Path = source.SOURCE_ROOT,
                        fetch: Callable[[str, Path, source.SourceLimits], None] = source.download,
                        output_max_bytes: int = 16 * 1024 * 1024) -> HanaRunOutcome:
    """Retain source and maker locks through shell preparation and result capture."""
    configuration = conf.read_or_repository('hana_aws')
    try:
        settings = read_settings(configuration)
        if request.argv != request.manifest.argv:
            raise ValueError('The command differs from its source manifest')
        with source.materialize(request.manifest, source.read_limits(configuration), root=root, fetch=fetch,
                showrunner='hana', unit=request.key.episode, work=request.key.attempt,
                selection=build_slots.ExactSlotRequest(request.slot)) as leased:
            if isinstance(leased, source.MissingBasis):
                return HanaExecutionRefused('MissingBasis: ' + json.dumps(
                    {'repository': leased.repository, 'commits': leased.commits}, separators=(',', ':')))
            execution = linux_runner.LeasedSlotExecution(leased.slot, leased.descriptors)
            shell = linux_runner.prepare_shell(leased.path, root / 'shells', settings.shell, settings.shell_timeout_s)
            result = linux_runner.run_slot(execution, request.argv, dict(request.manifest.environment), shell=shell,
                compiler_version=settings.compiler_version, free_floor_gib=settings.free_floor_gib,
                output_max_bytes=output_max_bytes, timeout_s=settings.command_timeout_s)
            return HanaCommandFinished(HanaCommandResult(request.key, request.source_key.key,
                leased.identity.commit, leased.identity.toolchain, socket.gethostname().split('.')[0],
                request.argv, leased.slot.number, result.status, result.stdout, result.stderr,
                result.started_at, result.finished_at, result.execution_seconds,
                PeakUnavailable('The command unit did not report peak memory')))
    except linux_runner.LeasedCommandLost as error:
        return HanaExecutionLost(str(error))
    except (OSError, ValueError, subprocess.SubprocessError, build_slots.SlotsFull,
            build_slots.SlotsNotConfigured, build_slots.SlotGitFailure) as error:
        return HanaExecutionRefused(str(error))


def encode_outcome(outcome: HanaRunOutcome) -> bytes:
    if isinstance(outcome, HanaCommandFinished):
        return b'{"finished":' + encode_result(outcome.result) + b'}'
    kind = 'refused' if isinstance(outcome, HanaExecutionRefused) else 'lost'
    return json.dumps({kind: outcome.reason}, separators=(',', ':')).encode()


def materialize_source_update(request: HanaExecutionRequest, *, root: Path = source.SOURCE_ROOT,
        fetch: Callable[[str, Path, source.SourceLimits], None] = source.download) -> HanaSourceUpdateOutcome:
    """Prepare a complete bundle without shell preparation or command execution."""
    try:
        if request.manifest.basis_commits or request.argv != request.manifest.argv:
            raise ValueError('The source update requires the complete original capture')
        limits = source.read_limits(conf.read_or_repository('hana_aws'))
        with source.materialize(request.manifest, limits, root=root, fetch=fetch,
                showrunner='hana', unit=request.key.episode, work=request.key.attempt,
                selection=build_slots.ExactSlotRequest(request.slot)) as leased:
            if isinstance(leased, source.MissingBasis):
                raise ValueError('The complete source update still requires basis commits')
            return HanaSourceUpdated(leased.identity)
    except (OSError, ValueError, subprocess.SubprocessError, build_slots.SlotsFull,
            build_slots.SlotsNotConfigured, build_slots.SlotGitFailure) as error:
        return HanaSourceUpdateRefused(str(error))


def encode_source_outcome(outcome: HanaSourceUpdateOutcome) -> bytes:
    if isinstance(outcome, HanaSourceUpdated):
        return json.dumps({'source_updated': asdict(outcome.identity)}, separators=(',', ':')).encode()
    return json.dumps({'refused': outcome.reason}, separators=(',', ':')).encode()


def decode_source_outcome(content: bytes) -> HanaSourceUpdateOutcome:
    from scripts.aws_target.hana_result import object_fields, string_field

    row = object_fields(cast(object, json.loads(content)))
    if set(row) == {'source_updated'}:
        identity = object_fields(row['source_updated'])
        return HanaSourceUpdated(source.SourceIdentity(string_field(identity, 'repository'),
            string_field(identity, 'commit'), string_field(identity, 'toolchain')))
    if set(row) == {'refused'}:
        return HanaSourceUpdateRefused(string_field(row, 'refused'))
    raise ValueError('The source update outcome is invalid')


def decode_outcome(content: bytes) -> HanaRunOutcome:
    from scripts.aws_target.hana_result import object_fields, string_field

    row = object_fields(cast(object, json.loads(content)))
    if set(row) == {'finished'}:
        return HanaCommandFinished(decode_result(json.dumps(row['finished']).encode()))
    if set(row) == {'refused'}:
        return HanaExecutionRefused(string_field(row, 'refused'))
    if set(row) == {'lost'}:
        return HanaExecutionLost(string_field(row, 'lost'))
    raise ValueError('The runner outcome is invalid')


def main() -> int:
    from scripts.aws_target.hana_execution import decode_request
    from scripts.aws_target.hana_result import object_fields, integer_field

    content = sys.stdin.buffer.read(source.MANIFEST_MAX_BYTES + 1)
    if len(content) > source.MANIFEST_MAX_BYTES:
        raise ValueError('The runner input exceeds the protocol bound')
    row = object_fields(cast(object, json.loads(content)))
    request = decode_request(json.dumps(row['request']).encode())
    # Source diagnostics must not change the result stream.
    with redirect_stdout(sys.stderr):
        if row.get('action', 'execute') == 'source-update':
            content = encode_source_outcome(materialize_source_update(request))
        elif row.get('action', 'execute') == 'execute':
            content = encode_outcome(materialize_and_run(request, output_max_bytes=integer_field(row, 'output_max_bytes')))
        else:
            raise ValueError('The runner action is invalid')
    _ = sys.stdout.buffer.write(content)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

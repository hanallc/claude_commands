"""Execute exact Hana source inside the unprivileged command unit."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import socket
import subprocess
import sys
from typing import cast

from scripts.aws_target import hana_protocol, hana_source as source
from scripts.aws_target.hana_result import HanaCommandResult, PeakUnavailable, integer_field, object_fields
from scripts.lib import build_folders, build_slots, conf
from scripts.mac_test import linux_runner

IMAGE_COMPILER_VERSION = Path('/etc/hana-aws/compiler-version')
SHELL_TIMEOUT_KEY = 'loopback.shell_timeout_s'
EXECUTION_SHELL_KEY = 'execution_shell'
FREE_FLOOR_KEY = 'free_floor_gib'
EXECUTION_CONFIG_KEYS = (EXECUTION_SHELL_KEY, FREE_FLOOR_KEY)


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
class GuestExecutionSettings:
    shell: str
    shell_timeout_s: float
    compiler_version: str
    free_floor_gib: float
    command_timeout_s: float


def read_settings(configuration: conf.ConfigRead, command_timeout_s: float) -> GuestExecutionSettings:
    values = configuration.values(*EXECUTION_CONFIG_KEYS)
    timeouts = conf.read_or_repository('build_targets').values(SHELL_TIMEOUT_KEY)
    settings = GuestExecutionSettings(configuration.text(EXECUTION_SHELL_KEY),
        linux_runner.positive_setting(timeouts, SHELL_TIMEOUT_KEY),
        read_image_compiler_version(),
        linux_runner.positive_setting(values, FREE_FLOOR_KEY),
        command_timeout_s)
    if not settings.shell or not settings.compiler_version:
        raise ValueError('The guest requires a shell and a compiler version')
    return settings


def materialize_and_run(request: hana_protocol.HanaExecutionRequest, verified_bundle: source.VerifiedGitBundle,
                        *, command_timeout_s: float,
                        output_max_bytes: int = 16 * 1024 * 1024, build_start_fd: int = -1) -> hana_protocol.HanaRunOutcome:
    """Retain source and maker locks through shell preparation and result capture."""
    configuration = conf.read_or_repository('hana_aws')
    try:
        settings = read_settings(configuration, command_timeout_s)
        if request.argv != request.manifest.argv:
            raise ValueError('The command differs from its source manifest')
        with source.materialize(request.manifest, source.read_limits(configuration), verified_bundle,
                                number=request.slot, work=request.key.attempt) as leased:
            if isinstance(leased, source.MissingBasis):
                return hana_protocol.HanaExecutionRefused('MissingBasis: ' + json.dumps(
                    {'repository': leased.repository, 'commits': leased.commits}, separators=(',', ':')))
            execution = linux_runner.LeasedSlotExecution(leased.copy, leased.descriptors)
            with build_folders.leased_build_folder(build_folders.MainOutput(leased.copy.repository)) as main:
                build_folders.mark_folder_use(main)
                shell = linux_runner.prepare_shell(leased.path, main / 'shells', settings.shell, settings.shell_timeout_s,
                    pass_fds=linux_runner.execution_descriptors())
                result = linux_runner.run_slot(execution, request.argv, dict(request.manifest.environment), shell=shell,
                    compiler_version=settings.compiler_version, free_floor_gib=settings.free_floor_gib,
                    output_max_bytes=output_max_bytes, timeout_s=settings.command_timeout_s, build_start_fd=build_start_fd)
            return hana_protocol.HanaCommandFinished(HanaCommandResult(request.key, request.source_key.key,
                leased.identity.commit, leased.identity.toolchain, socket.gethostname().split('.')[0],
                request.argv, leased.copy.number, result.status, result.stdout, result.stderr,
                result.started_at, result.finished_at, result.execution_seconds,
                PeakUnavailable('The command unit did not report peak memory')))
    except linux_runner.LeasedCommandLost as error:
        return hana_protocol.HanaExecutionLost(str(error))
    except (OSError, ValueError, subprocess.SubprocessError, build_slots.SlotGitFailure) as error:
        return hana_protocol.HanaExecutionRefused(str(error))


def materialize_source_update(request: hana_protocol.HanaExecutionRequest,
        verified_bundle: source.VerifiedGitBundle) -> hana_protocol.HanaSourceUpdateOutcome:
    """Prepare a complete bundle without shell preparation or command execution."""
    try:
        if request.manifest.basis_commits or request.argv != request.manifest.argv:
            raise ValueError('The source update requires the complete original capture')
        limits = source.read_limits(conf.read_or_repository('hana_aws'))
        with source.materialize(request.manifest, limits, verified_bundle,
                                number=request.slot, work=request.key.attempt) as leased:
            if isinstance(leased, source.MissingBasis):
                raise ValueError('The complete source update still requires basis commits')
            return hana_protocol.HanaSourceUpdated(leased.identity)
    except (OSError, ValueError, subprocess.SubprocessError, build_slots.SlotGitFailure) as error:
        return hana_protocol.HanaSourceUpdateRefused(str(error))


class RunnerArguments(argparse.Namespace):
    bundle_fd: int = -1
    bundle_offset: int = -1
    bundle_size: int = 0
    command_timeout_s: float = 0.0
    build_start_fd: int = -1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('--bundle-fd', required=True, type=int)
    _ = parser.add_argument('--bundle-offset', required=True, type=int)
    _ = parser.add_argument('--bundle-size', required=True, type=int)
    _ = parser.add_argument('--command-timeout-s', required=True, type=float)
    _ = parser.add_argument('--build-start-fd', type=int)
    arguments = parser.parse_args(namespace=RunnerArguments())
    if arguments.bundle_fd < 0 or arguments.bundle_offset < 0 or arguments.bundle_size <= 0:
        parser.error('The verified bundle range is invalid')
    if not math.isfinite(arguments.command_timeout_s) or arguments.command_timeout_s <= 0:
        parser.error('The image command timeout must be positive')
    verified_bundle = source.VerifiedGitBundle(arguments.bundle_fd, arguments.bundle_offset, arguments.bundle_size)
    content = sys.stdin.buffer.read(hana_protocol.CONTROL_MAX_BYTES + 1)
    if len(content) > hana_protocol.CONTROL_MAX_BYTES:
        raise ValueError('The runner input exceeds the protocol bound')
    row = object_fields(cast(object, json.loads(content)))
    request = hana_protocol.decode_request(json.dumps(row['request']).encode())
    # Source diagnostics must not change the result stream.
    with redirect_stdout(sys.stderr):
        if row.get('action', 'execute') == 'source-update':
            content = hana_protocol.encode_source_outcome(materialize_source_update(request, verified_bundle))
        elif row.get('action', 'execute') == 'execute':
            outcome = materialize_and_run(request, verified_bundle, command_timeout_s=arguments.command_timeout_s,
                                          output_max_bytes=integer_field(row, 'output_max_bytes'),
                                          build_start_fd=arguments.build_start_fd)
            content = hana_protocol.encode_outcome(outcome)
        else:
            raise ValueError('The runner action is invalid')
    _ = sys.stdout.buffer.write(content)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

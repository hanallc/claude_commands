"""Prepare local GPU trials and run their scaler service."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import base64
import inspect
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, ExitStack, redirect_stdout
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tomllib
from typing import cast, final
from uuid import uuid4

from scripts.aws_target import cloud_gate, ledger, run_cost, s3_presign
from scripts.aws_target.aws_cli import AwsExecutor, aws_error_code, execute_command
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable, control_lock, largest_gate_vcpus, spot_quota
from scripts.aws_target.config import gate_deployment, read_settings
from scripts.build_targets.target_config import ssh_target_state_directory
from scripts.lib import conf
from scripts.production import merge_gate
from scripts.production.gate_modules import GateRefused


ROOT = Path(__file__).resolve().parents[2]
SYSTEM_MODULE = ROOT / 'docs/plans/builds-aws/gpu-trial.nix'
WRAPPER = Path(__file__).with_name('gpu_trial_wrapper.py')
ARCHIVE_PROFILE = 'gpu-size-trial-archive'
NEXTEST_INFRASTRUCTURE_EXIT = 125
NEXTEST_LOG_TAIL_BYTES = 1024 * 1024
NEXTEST_LOG_TAIL_CHUNK_BYTES = 64 * 1024
# Use the upper limits for expiry_schedule_minutes and expiry_timeout_s in cloud_gate.conf.
MAX_GATE_EXPIRY_INTERVAL_MINUTES = 60
MAX_GATE_EXPIRY_FUNCTION_SECONDS = 60
EVENTBRIDGE_DELIVERY_MARGIN_SECONDS = 60
# Lambda retries function errors twice, after delays of one and two minutes.
# https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html
EXPIRY_RETRY_DELAYS_SECONDS = (60, 120)
HANA_EXPIRY_MARGIN_SECONDS = (
    MAX_GATE_EXPIRY_INTERVAL_MINUTES * 60
    + (1 + len(EXPIRY_RETRY_DELAYS_SECONDS)) * MAX_GATE_EXPIRY_FUNCTION_SECONDS
    + sum(EXPIRY_RETRY_DELAYS_SECONDS)
    + EVENTBRIDGE_DELIVERY_MARGIN_SECONDS
)
SPOT_QUOTA_REFUSALS = frozenset({'MaxSpotInstanceCountExceeded', 'VcpuLimitExceeded'})
# Use EC2 errors that reject the request.
RUN_INSTANCES_REFUSALS = frozenset({
    'UnauthorizedOperation', 'AuthFailure', 'InvalidParameter', 'InvalidParameterValue',
    'InvalidParameterCombination', 'MissingParameter', 'Unsupported', 'InstanceLimitExceeded',
    'InsufficientInstanceCapacity',
}) | SPOT_QUOTA_REFUSALS
PREPARED_NAMES = frozenset(('hana-tests.tar.zst', 'closure.export', 'source.tar', 'cargo-home',
    'runtime.env', 'gpu-trial.nix', 'closure.json', 'request.json', 'manifest.json', 'inventory.json',
    'gpu_trial_wrapper.py', 'machines', 'prepare.log'))
RUN_INPUT_NAMES = ('hana-tests.tar.zst', 'closure.export', 'source.tar', 'payload.tar', 'cargo-home')


@dataclass(frozen=True)
class TrialLimits:
    limit_usd: Decimal
    lifetime_minutes: int
    box_extra_usd: Decimal
    spot_max_usd_per_hour: dict[str, Decimal]


@dataclass(frozen=True)
class GpuSizeComparison:
    pass


GPU_SIZE_COMPARISON = GpuSizeComparison()


@dataclass(frozen=True)
class HanaGpuNextestRequest:
    arguments: tuple[str, ...]
    instance_types: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.arguments or not self.instance_types or len(set(self.instance_types)) != len(self.instance_types):
            raise ValueError('Supply nextest arguments and a list of distinct Hana GPU types')
        _ = nextest_name_filters(self.arguments)


def nextest_name_filters(arguments: Sequence[str]) -> list[tuple[str, bool]]:
    """Require an explicit selection and return its test name filters."""
    selectors = {'--test', '--bin', '--example', '--bench', '--filterset', '-E'}
    values = selectors | {
        '--package', '-p', '--exclude', '--features', '-F', '--cargo-profile', '--target',
        '--cargo-message-format', '-Z', '--run-ignored', '--partition', '--platform-filter',
        '--test-threads', '--jobs', '-j', '--retries', '--flaky-result', '--max-fail',
        '--failure-output', '--success-output', '--status-level', '--final-status-level',
        '--show-progress', '--profile', '-P',
    }
    reserved = {
        '--archive-file', '--binaries-metadata', '--cargo-metadata', '--config',
        '--manifest-path', '--target-dir', '--build-jobs', '--unit-graph',
        '--help', '-h', '--version', '-V', '--no-run', '--no-tests', '--list',
        '--message-format', '--message-format-version', '--color', '--no-capture', '--nocapture',
        '--debugger', '--tracer', '--stress-count', '--stress-duration', '--rerun', '-R',
    }
    names: list[tuple[str, bool]] = []
    selected = False
    parts = iter(arguments)
    for argument in parts:
        if argument == '--':
            binary_args = tuple(parts)
            exact = '--exact' in binary_args
            binary_parts = iter(binary_args)
            for word in binary_parts:
                option, separator, _value = word.partition('=')
                if option in {'--list', '--help', '-h', '--nocapture', '--no-capture'}:
                    raise ValueError('The Hana GPU run does not accept ' + option)
                if option == '--skip' and not separator:
                    _ = next(binary_parts, '')
                elif word.strip() and not word.startswith('-'):
                    names.append((word, exact))
            break
        option, separator, value = argument.partition('=')
        if not argument.startswith('-'):
            if argument.strip():
                names.append((argument, False))
            continue
        if not option.startswith('--') and len(option) > 2 and option[:2] in values | reserved:
            option, separator, value = option[:2], '=', argument[2:]
        if option in reserved:
            raise ValueError('The Hana GPU run does not accept ' + option)
        if option in values:
            if not separator:
                value = next(parts, '')
            if not value.strip() or value.startswith('-'):
                raise ValueError('The Hana GPU run needs a value for ' + option)
            selected = selected or option in selectors
    if not (selected or names):
        raise ValueError('Supply an explicit nextest test filter')
    return names


@dataclass(frozen=True)
class HanaGpuNextestResult:
    attempt: str
    commit: str
    tree: str
    exit_code: int
    stdout: str
    stderr: str


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError('Expected an object with text keys')
    if any(not isinstance(key, str) for key in cast(dict[object, object], value)):
        raise ValueError('Expected an object with text keys')
    return cast(dict[str, object], value)


def read_json(path: Path) -> dict[str, object]:
    return mapping(cast(object, json.loads(path.read_text())))


def write_json(path: Path, value: object) -> None:
    _ = path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def price_caps(text: str) -> dict[str, Decimal]:
    prices = mapping(cast(object, json.loads(text)))
    maximums: dict[str, Decimal] = {}
    for name, value in prices.items():
        price = conf.decimal(str(value))
        if not 0 < price <= 10:
            raise ValueError('GPU trial prices must be greater than zero and at most 10 USD/hour')
        maximums[name] = price
    if not maximums:
        raise ValueError('The GPU trial needs at least one type')
    return maximums


def settings() -> TrialLimits:
    source = conf.read_or_repository('cloud_gate')
    maximums = price_caps(source.text('gpu_size_trial_spot_max_usd_per_hour'))
    for name in maximums:
        if not re.fullmatch(r'(?:g6\.(?:[248]xlarge|xlarge)|g4dn\.xlarge)', name):
            raise ValueError('The GPU trial has an unsupported type: ' + name)
    return TrialLimits(conf.decimal(source.text('gpu_size_trial_limit_usd')),
                       source.whole('gpu_size_trial_lifetime_minutes'),
                       conf.decimal(source.text('gpu_size_trial_box_extra_usd')), maximums)


def hana_nextest_limits(request: HanaGpuNextestRequest) -> TrialLimits:
    source = conf.read_or_repository('cloud_gate')
    types = source.words('hana_gpu_instance_types')
    maximums = price_caps(source.text('hana_gpu_spot_max_usd_per_hour'))
    for size in request.instance_types:
        if (size not in types or not re.fullmatch(r'(?:g4dn|g5|g6|g6e)\.[1-9][0-9]*xlarge', size)
                or largest_gate_vcpus((size,)) < 8 or size not in maximums):
            raise ValueError('No eligible Hana GPU type and price cap for ' + size)
    return TrialLimits(conf.decimal(source.text('gpu_size_trial_limit_usd')),
                       source.whole('hana_gpu_lifetime_minutes'),
                       conf.decimal(source.text('gpu_size_trial_box_extra_usd')),
                       {size: maximums[size] for size in request.instance_types})


def string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in cast(list[object], value)):
        raise ValueError('The Hana GPU request needs a list of strings')
    return tuple(cast(list[str], value))


def nextest_request(document: dict[str, object]) -> GpuSizeComparison | HanaGpuNextestRequest:
    if 'hana_nextest' not in document:
        return GPU_SIZE_COMPARISON
    request = mapping(document['hana_nextest'])
    return HanaGpuNextestRequest(string_list(request['arguments']), string_list(request['instance_types']))


def check_nextest_pass(stdout: str, stderr: str, arguments: Sequence[str]) -> None:
    """Require one successful test event for each test selected by nextest."""
    planned = list(re.finditer(r'^ +Starting ([1-9][0-9]*) tests? across ', stderr, re.MULTILINE))
    started: set[str] = set()
    passed: set[str] = set()
    reported = 0
    for line in stdout.splitlines():
        event = mapping(cast(object, json.loads(line)))
        kind, status = event.get('type'), event.get('event')
        if kind == 'test' and status in ('started', 'ok'):
            name = event.get('name')
            seen = started if status == 'started' else passed
            if not isinstance(name, str) or '$' not in name:
                raise ValueError('The Hana GPU result has invalid test evidence')
            if status == 'ok' and name not in started:
                name = re.sub(r'#(?:[2-9]|[1-9][0-9]+)$', '', name)
            if name in seen:
                raise ValueError('The Hana GPU result has invalid test evidence')
            seen.add(name)
        elif kind == 'suite' and status == 'ok':
            count = event.get('passed')
            if type(count) is not int or count < 0:
                raise ValueError('The Hana GPU result has invalid test evidence')
            reported += count
        elif status == 'failed':
            raise ValueError('The Hana GPU result reports a failed test')
    if len(planned) != 1 or len(passed) != int(planned[0][1]) or reported != len(passed) or not passed <= started:
        raise ValueError('The Hana GPU result does not confirm that all selected tests passed')
    test_names = [name.split('$', 1)[1] for name in passed]
    for name, exact in nextest_name_filters(arguments):
        if not any(name == test if exact else name in test for test in test_names):
            raise ValueError('The Hana GPU result has no passing test for the filter ' + name)


def nextest_result(document: dict[str, object], attempt: str, manifest: Mapping[str, object]) -> HanaGpuNextestResult:
    if document.get('status') != 'complete':
        raise ValueError('The Hana GPU run did not complete: ' + str(document.get('reason', 'unknown reason')))
    result = mapping(document['nextest'])
    commit, tree = manifest['commit'], manifest['tree']
    if result.get('attempt') != attempt or result.get('commit') != commit or result.get('tree') != tree:
        raise ValueError('The Hana GPU result has the wrong attempt or source identity')
    code, stdout, stderr = result.get('exit_code'), result.get('stdout'), result.get('stderr')
    if not isinstance(code, int) or isinstance(code, bool) or not 0 <= code <= 255 or not isinstance(stdout, str) or not isinstance(stderr, str):
        raise ValueError('The Hana GPU result has an invalid exit status or output')
    if code == 4:
        raise ValueError('Cargo nextest selected no tests')
    if code == 0:
        arguments = string_list(mapping(manifest['hana_nextest'])['arguments'])
        check_nextest_pass(stdout, stderr, arguments)
    return HanaGpuNextestResult(attempt, str(commit), str(tree), code, stdout, stderr)


def required_vcpus(limits: TrialLimits) -> int:
    """Derive the trial quota from every configured launch type."""
    needed = 0
    for size in limits.spot_max_usd_per_hour:
        vcpus = largest_gate_vcpus((size,))
        if not vcpus:
            raise ValueError('No vCPU count is available for configured GPU type: ' + size)
        needed = max(needed, vcpus)
    return needed


def digest(path: Path) -> str:
    with path.open('rb') as content:
        return hashlib.file_digest(content, 'sha256').hexdigest()


def checked(arguments: Sequence[str], cwd: Path, *, environment: Mapping[str, str] = os.environ,
            timeout: float | None = None, capture_stderr: bool = False) -> str:
    result = subprocess.run(arguments, cwd=cwd, env=environment, check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE if capture_stderr else None,
                            text=True, timeout=timeout)
    return result.stdout.strip()


def hardware_vulkan(payload: str) -> tuple[dict[str, str], ...]:
    """Require a hardware GPU and record all adapter names and types."""
    adapters: list[dict[str, str]] = []
    for line in payload.splitlines():
        key, separator, value = line.partition('=')
        if not separator:
            continue
        if key.strip() == 'deviceType':
            adapters.append({'type': value.strip(), 'name': ''})
        elif key.strip() == 'deviceName' and adapters:
            adapters[-1]['name'] = value.strip()
    if not any(adapter['type'] in ('PHYSICAL_DEVICE_TYPE_DISCRETE_GPU',
                                   'PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU',
                                   'PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU') for adapter in adapters):
        raise ValueError('The trial needs at least one hardware Vulkan adapter')
    return tuple(adapters)


def trial_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in ('VK_DRIVER_FILES', 'VK_ICD_FILENAMES', 'LIBGL_ALWAYS_SOFTWARE',
                'LIBGL_DRIVERS_PATH', '__EGL_VENDOR_LIBRARY_FILENAMES', 'CARGO_TARGET_DIR',
                'RUSTUP_HOME', 'RUSTC_WRAPPER', 'RUSTC_WORKSPACE_WRAPPER', 'CARGO_BUILD_RUSTC_WRAPPER'):
        _ = environment.pop(key, None)
    environment.update(WGPU_BACKEND='vulkan', CARGO_NET_OFFLINE='true',
                       CARGO_INCREMENTAL='0', RUSTFLAGS='-C target-cpu=x86-64',
                       CARGO_BUILD_TARGET='x86_64-unknown-linux-gnu')
    _ = environment.pop('CARGO_ENCODED_RUSTFLAGS', None)
    return environment


def preflight(source: Path, environment: Mapping[str, str], *, guest_only: bool = False) -> dict[str, str]:
    """Check external tools before admission without building the Rust toolchain."""
    timeout = conf.read_or_repository('aws_target').number('gpu_size_trial_preflight_timeout_s')
    tools: dict[str, str] = {}
    specifications = (
        ('git', False, ('--version',)),
        ('nix', False, ('--version',)),
        ('nix-store', False, ('--version',)),
        ('python', False, ('--version',)),
        ('sh', True, ('-c', ':')),
        ('cargo', True, ('--version',)),
        ('rustc', True, ('--version',)),
        ('rustdoc', True, ('--version',)),
        ('cargo-nextest', True, ('--version',)),
        ('zstd', True, ('--version',)),
        ('vulkaninfo', True, ('--summary',)),
    )
    for name, in_shell, arguments in specifications:
        if guest_only and name in ('zstd', 'vulkaninfo'):
            continue
        context = name + (' in the dev shell' if in_shell else '')
        try:
            prefix = [tools['nix'], 'develop', '--no-write-lock-file', '.#default', '-c'] if in_shell else []
            if name == 'vulkaninfo':
                tools['vulkan_tools'] = checked(
                    [tools['nix'], 'build', '--no-link', '--print-out-paths', '--inputs-from',
                     str(source), 'nixpkgs#vulkan-tools'], source, environment=environment, timeout=timeout)
                if not re.fullmatch(r'/nix/store/[A-Za-z0-9+._?-]+', tools['vulkan_tools']):
                    raise ValueError('vulkan-tools must resolve to one Nix store path')
                path = tools['vulkan_tools'] + '/bin/vulkaninfo'
            elif in_shell:
                path = checked([*prefix, 'sh', '-c', 'command -v "$1"', 'gpu-trial-preflight', name],
                               source, environment=environment, timeout=timeout)
            else:
                path = sys.executable if name == 'python' else shutil.which(name, path=environment.get('PATH'))
            if not path:
                raise ValueError('The tool is missing')
            tools[name] = path
            payload = checked([*prefix, path, *arguments], source, environment=environment, timeout=timeout)
            if name == 'vulkaninfo':
                _ = hardware_vulkan(payload)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise ValueError(f'GPU trial preflight failed for {context}: {error}') from error
    return tools


def trial_root() -> Path:
    return Path.home() / '.local/state/gpu-size-trial'


def host_trial_folder(folder: Path) -> Path:
    """Require a canonical direct child of the host trial root."""
    path = folder.expanduser().absolute()
    resolved = path.resolve()
    if path != resolved or resolved.parent != trial_root().resolve() or resolved.name == '.locks':
        raise ValueError('The host trial folder must be a canonical direct child of the trial root without directory links')
    return resolved


def remove_path(path: Path) -> None:
    """Remove owned output without following a directory link."""
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def trial_lock_directory(folder: Path) -> Path:
    key = hashlib.sha256(str(folder.resolve()).encode()).hexdigest()
    return trial_root() / '.locks' / key


def cleanup_receipt(folder: Path) -> Path:
    """Keep cleanup evidence when the prepared trial is pruned."""
    return trial_lock_directory(folder) / 'cleanup-confirmed.json'


@contextmanager
def trial_lock(folder: Path, *, exclusive: bool = False) -> Generator[None]:
    directory = trial_lock_directory(folder)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def held_trial(folder: Path) -> Generator[None]:
    folder = host_trial_folder(folder)
    with ExitStack() as held:
        with control_lock(trial_root()):
            if not folder.is_dir() or folder.is_symlink():
                raise ValueError('The trial folder is absent or is a directory link')
            _ = held.enter_context(trial_lock(folder))
        yield


@contextmanager
def new_trial(folder: Path, *, prune_previous: bool = True) -> Generator[None]:
    folder = host_trial_folder(folder)
    root = trial_root()
    with ExitStack() as held:
        with control_lock(root):
            if folder.exists() or folder.is_symlink():
                raise FileExistsError('The new trial folder already exists: ' + str(folder))
            if prune_previous:
                previous_folders = [host_trial_folder(path) for path in sorted(root.iterdir())
                                    if path.name != '.locks' and (path.is_dir() or path.is_symlink())]
                busy = False
                for previous in previous_folders:
                    try:
                        with trial_lock(previous, exclusive=True):
                            _ = host_trial_folder(previous)
                            remove_path(previous)
                    except BlockingIOError:
                        busy = True
                if busy:
                    raise ValueError('A GPU trial folder is held by a live run; finish that run before preparing another trial')
            _ = held.enter_context(trial_lock(folder))
            folder.mkdir(parents=True, exist_ok=False)
        yield


def remove_run_inputs(folder: Path, at: float) -> None:
    folder = host_trial_folder(folder)
    cloud_gate.save(folder / 'inputs-removed.json', {'at': at})
    for name in RUN_INPUT_NAMES:
        remove_path(folder / name)


def prepare(repo: Path, commit: str, folder: Path) -> None:
    with new_trial(folder):
        prepare_locked(repo, commit, folder)


def prepare_locked(repo: Path, commit: str, folder: Path,
                   mode: GpuSizeComparison | HanaGpuNextestRequest = GPU_SIZE_COMPARISON) -> None:
    """Check external tools, then admit the local preparation worker."""
    folder = host_trial_folder(folder)
    log = folder / 'prepare.log'
    try:
        log.touch()
        limits = hana_nextest_limits(mode) if isinstance(mode, HanaGpuNextestRequest) else settings()
        resolved = checked(['git', 'rev-parse', '--verify', commit + '^{commit}'], repo)
        if not re.fullmatch('[0-9a-f]{40}', resolved):
            raise ValueError('The trial needs a resolved Git commit')
        source = export_source(repo, resolved, folder)
        nextest = isinstance(mode, HanaGpuNextestRequest)
        environment = trial_environment()
        tools = preflight(source, environment, guest_only=True) if nextest else preflight(source, environment)
        request: dict[str, object] = {'repo': str(repo), 'commit': resolved, 'tools': tools,
            'limits': {'limit_usd': str(limits.limit_usd), 'lifetime_minutes': limits.lifetime_minutes,
                       'box_extra_usd': str(limits.box_extra_usd),
                       'spot_max_usd_per_hour': {key: str(value) for key, value in limits.spot_max_usd_per_hour.items()}}}
        if isinstance(mode, HanaGpuNextestRequest):
            request['hana_nextest'] = asdict(mode)
        write_json(folder / 'request.json', request)
        hold = merge_gate.BuildHoldWait()
        hold.wait_until_free()
        with merge_gate.gate_reservation(repo, folder, hold, mode=merge_gate.SuiteMode.TARGETED_TEST) as place:
            hold.wait_until_free()
            command = [sys.executable, '-m', 'scripts.aws_target.gpu_size_trial', '--prepare-worker', str(folder)]
            with log.open('wb') as output:
                process = place.popen(command, cwd=ROOT, env=os.environ, stdout=output, stderr=subprocess.STDOUT)
                status = process.wait()
            if status:
                raise GateRefused(f'GPU trial preparation failed with exit {status}: {log}')
    except Exception as error:
        _ = host_trial_folder(folder)
        with log.open('a') as output:
            _ = output.write(str(error) + '\n')
        append_failed_nextest_logs(folder)
        for path in folder.iterdir():
            if path == log:
                continue
            remove_path(path)
        raise
    if isinstance(mode, GpuSizeComparison):
        print(folder)


def nextest_log_tail(path: Path) -> str:
    """Read up to 200 final lines within a fixed byte limit."""
    chunks: list[bytes] = []
    newlines = 0
    with path.open('rb') as content:
        position = content.seek(0, os.SEEK_END)
        remaining = min(position, NEXTEST_LOG_TAIL_BYTES)
        while remaining and newlines <= 200:
            size = min(remaining, NEXTEST_LOG_TAIL_CHUNK_BYTES)
            position -= size
            _ = content.seek(position)
            chunk = content.read(size)
            chunks.append(chunk)
            newlines += chunk.count(b'\n')
            remaining -= size
    text = b''.join(reversed(chunks)).decode('utf-8', errors='replace')
    return ''.join(text.splitlines(keepends=True)[-200:])


def append_failed_nextest_logs(folder: Path) -> None:
    """Keep the last 200 lines of failed nextest logs before preparation removes them."""
    with (folder / 'prepare.log').open('a') as output:
        for diagnostic in sorted(folder.glob('machines/*/*/nextest.log')):
            try:
                if read_json(diagnostic.with_name('run.json')).get('status') == 0:
                    continue
            except (OSError, ValueError):
                # Keep the diagnostic when the runner did not write a valid status.
                pass
            _ = output.write('\nNextest diagnostic: ' + str(diagnostic.relative_to(folder)) + ' (last 200 lines)\n')
            try:
                _ = output.write(nextest_log_tail(diagnostic) + '\n')
            except OSError as error:
                _ = output.write('Could not read the nextest diagnostic: ' + str(error) + '\n')


def export_source(repo: Path, commit: str, folder: Path) -> Path:
    source = folder / 'source'
    source.mkdir()
    archive = folder / 'source.tar'
    with archive.open('wb') as output:
        _ = subprocess.run(['git', 'archive', '--format=tar', commit], cwd=repo, stdout=output, check=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(source, filter='data')
    return source


def nix_string(value: str) -> str:
    return json.dumps(value).replace('${', '\\${')


def scan_toolchain_wrappers(toolchain: Path) -> None:
    """Refuse wrappers that depend on files in a user's home."""
    for path in (toolchain / 'bin').iterdir():
        if not path.is_file():
            continue
        with path.open('rb') as content:
            prefix = content.read(1024 * 1024)
        if prefix.startswith(b'#!') and any(value in prefix for value in (
                b'/home/', b'/Users/', b'/.rustup', b'cargo-tile-capture', str(Path.home()).encode())):
            raise ValueError('The Nix toolchain wrapper depends on a user home: ' + path.name)


def verify_hana_nextest(source: Path, version: str) -> None:
    workflow = (source / '.github/workflows/ci.yml').read_text()
    pins = list(re.finditer(r"""^  HANA_NEXTEST_VERSION: (["']?)([0-9]+\.[0-9]+\.[0-9]+)\1 *$""",
                            workflow, re.MULTILINE))
    if len(pins) != 1:
        raise ValueError('The Hana CI workflow must specify one HANA_NEXTEST_VERSION')
    pinned = pins[0][2]
    if not re.match(r'^cargo-nextest ' + re.escape(pinned) + r'(?:\s|$)', version):
        raise ValueError('Cargo nextest does not match the Hana CI pin ' + pinned)


def nix_toolchain(folder: Path, source: Path, environment: Mapping[str, str], *, hana_nextest: bool = False) -> str:
    nextest = '''
        nextest = pkgs.runCommand "cargo-nextest-0.9.143" {
          src = pkgs.fetchurl {
            url = "https://github.com/nextest-rs/nextest/releases/download/cargo-nextest-0.9.143/cargo-nextest-0.9.143-x86_64-unknown-linux-musl.tar.gz";
            sha256 = "6d891c18105ec2d33f6e441a4f92b7ccab47ba263e22055c8bb66884243f3389";
          };
        } ''
          mkdir -p "$out/bin"
          tar -xzf "$src" -C "$out/bin" cargo-nextest
        '';
    ''' if hana_nextest else ''
    paths = '[ nextest chosen ]' if hana_nextest else '[ chosen pkgs.cargo-nextest ]'
    expression = ('let host = builtins.getFlake "path:/etc/nixos"; '
        + 'hana = builtins.getFlake ' + nix_string('path:' + str(source)) + '; '
        + 'pkgs = import host.inputs.nixpkgs { system = "x86_64-linux"; overlays = [ '
        + 'host.inputs.lanzaboote.inputs.rust-overlay.overlays.default ]; }; '
        + 'fallback = if builtins.pathExists ' + nix_string(str(source / 'rust-toolchain.toml'))
        + ' then pkgs.rust-bin.fromRustupToolchainFile (builtins.toPath '
        + nix_string(str(source / 'rust-toolchain.toml')) + ') else pkgs.symlinkJoin { '
        + 'name = "gpu-trial-toolchain"; paths = [ pkgs.rustc pkgs.cargo ]; }; '
        + 'chosen = hana.packages.x86_64-linux.rust-toolchain or (hana.packages.x86_64-linux.toolchain or fallback); '
        + nextest + 'in pkgs.symlinkJoin { name = "gpu-trial-toolchain-with-nextest"; paths = ' + paths + '; }')
    toolchain = checked([environment.get('GPU_TRIAL_NIX', 'nix'), 'build', '--impure', '--out-link', str(folder / 'toolchain-profile'),
                         '--print-out-paths', '--expr', expression], folder,
                        environment=environment)
    if not re.fullmatch(r'/nix/store/[A-Za-z0-9+._?-]+', toolchain):
        raise ValueError('The trial toolchain must come from the Nix store')
    scan_toolchain_wrappers(Path(toolchain))
    empty_home = folder / 'toolchain-probe-home'
    empty_home.mkdir()
    _ = checked([toolchain + '/bin/cargo', '--version'], source,
                environment={**environment, 'HOME': str(empty_home), 'CARGO_HOME': str(empty_home),
                             'RUSTUP_HOME': str(empty_home)})
    return toolchain


def build_system(folder: Path, source: Path, environment: Mapping[str, str], toolchain: str, *,
                 hana_nextest: bool = False) -> str:
    """Build the GPU system from the deployment host's locked nixpkgs input."""
    nix = environment.get('GPU_TRIAL_NIX', 'nix')
    nix_store = environment.get('GPU_TRIAL_NIX_STORE', 'nix-store')
    nixpkgs = checked([nix, 'eval', '--impure', '--raw', '--expr',
                      '(builtins.getFlake "path:/etc/nixos").inputs.nixpkgs.outPath'], folder, environment=environment)
    runtime = folder / 'runtime-profile'
    shell = checked([nix, 'print-dev-env', '--no-write-lock-file', '--profile', str(runtime), '.#default'], source,
                    environment=environment)
    _ = (folder / 'runtime.env').write_text(shell + '\n')
    module = folder / 'gpu-trial.nix'
    _ = shutil.copyfile(SYSTEM_MODULE, module)
    expression = ('let pkgsPath = builtins.toPath ' + nix_string(nixpkgs) + '; in '
                  + '(import (pkgsPath + "/nixos/lib/eval-config.nix") { system = "x86_64-linux"; modules = [ '
                  + '(import (builtins.toPath ' + nix_string(str(module)) + ') { runtimeRoots = [ (builtins.toPath '
                  + nix_string(str(runtime.resolve())) + ') ]; toolchain = builtins.toPath '
                  + nix_string(toolchain) + '; hanaNextest = ' + ('true' if hana_nextest else 'false')
                  + '; }) ]; }).config.system.build.toplevel')
    system = checked([nix, 'build', '--impure', '--out-link', str(folder / 'system-profile'),
                      '--print-out-paths', '--expr', expression], folder,
                     environment=environment)
    roots = checked([nix_store, '--query', '--requisites', system, str(runtime.resolve()), toolchain], folder,
                    environment=environment).splitlines()
    with (folder / 'closure.export').open('wb') as output:
        _ = subprocess.run([nix_store, '--export', *sorted(set(roots))], stdout=output, check=True, env=environment)
    write_json(folder / 'closure.json', {'system': system, 'toolchain': toolchain,
                                        'nixpkgs': nixpkgs, 'roots': roots})
    return system


def cache_dependencies(folder: Path, source: Path, environment: Mapping[str, str]) -> None:
    destination = folder / 'cargo-home'
    destination.mkdir()
    online = {**environment, 'CARGO_HOME': str(destination), 'CARGO_NET_OFFLINE': 'false'}
    _ = checked([environment.get('GPU_TRIAL_NIX', 'nix'), 'develop', '--no-write-lock-file', '.#default', '-c',
                 environment['GPU_TRIAL_CARGO'], 'fetch', '--locked'], source, environment=online)
    _ = (destination / 'config.toml').write_text('[net]\noffline = true\n')


def inventory(payload: dict[str, object]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for binary, raw in mapping(payload['rust-suites']).items():
        suite = mapping(raw)
        for name, item in mapping(suite['testcases']).items():
            case = mapping(item)
            if case.get('ignored') is False and mapping(case['filter-match']).get('status') == 'matches':
                result.append({'binary': binary, 'test': name})
    if not result:
        raise ValueError('The archive contains no selected tests')
    return result


def archive_arguments(folder: Path, source: Path) -> list[str]:
    return ['--archive-file', str(folder / 'hana-tests.tar.zst'), '--workspace-remap', str(source),
            '--profile', 'ci']


def archive_config(source: Path, target: Path) -> str:
    """Add target shared libraries to nextest's archive include list."""
    original = mapping(cast(object, tomllib.loads((source / '.config/nextest.toml').read_text())))
    profiles = mapping(original.get('profile', {}))
    defaults = mapping(mapping(profiles.get('default', {})).get('archive', {}))
    archive = mapping(mapping(profiles.get('ci', {})).get('archive', defaults))
    includes = [mapping(entry) for entry in cast(list[object], archive.get('include', []))]
    paths = {str(entry.get('path', '')) for entry in includes}
    for library in sorted(target.rglob('*.so*')):
        if library.is_file():
            path = library.relative_to(target).as_posix()
            if path not in paths:
                includes.append({'path': path, 'relative-to': 'target', 'on-missing': 'error'})
                paths.add(path)
    return '[profile.ci]\n[profile.' + ARCHIVE_PROFILE + ']\ninherits = "ci"\n[profile.' + ARCHIVE_PROFILE + '.archive]\ninclude = [\n' + ''.join(
        '  { ' + ', '.join(json.dumps(key) + ' = ' + json.dumps(value) for key, value in entry.items()) + ' },\n'
        for entry in includes) + ']\n'


def archive_tests(folder: Path, source: Path, environment: Mapping[str, str], prefix: Sequence[str]) -> list[dict[str, str]]:
    """Build and archive tests, then verify their shared libraries before closure work."""
    cargo = environment.get('GPU_TRIAL_CARGO', 'cargo')
    selection = ['--locked', '--workspace', '--all-features', '--tests']
    binaries = mapping(cast(object, json.loads(checked(
        [*prefix, cargo, 'nextest', 'list', '--profile', 'ci', *selection,
         '--list-type', 'binaries-only', '--message-format', 'json'],
        source, environment=environment))))
    target = Path(str(mapping(binaries['rust-build-meta'])['target-directory']))
    config = folder / 'archive-nextest.toml'
    _ = config.write_text(archive_config(source, target))
    _ = checked([*prefix, cargo, 'nextest', 'archive', '--profile', ARCHIVE_PROFILE, *selection,
                 '--config-file', str(source / '.config/nextest.toml'), '--tool-config-file', 'gpu-size-trial:' + str(config),
                 '--archive-file', str(folder / 'hana-tests.tar.zst')], source, environment=environment)
    try:
        listed = checked([*prefix, cargo, 'nextest', 'list', *archive_arguments(folder, source),
                          '--message-format', 'json'], source, environment=environment, capture_stderr=True)
    except subprocess.CalledProcessError as error:
        diagnostic = cast(str | None, error.stderr)
        raise ValueError('Nextest could not list the archived tests: ' + (diagnostic or str(error))) from error
    return inventory(mapping(cast(object, json.loads(listed))))


def junit_config(text: str, path: Path) -> str:
    section = re.compile(r'(?ms)^\[profile\.ci\.junit\]\s*\n(.*?)(?=^\[|\Z)')
    value = 'path = ' + json.dumps(str(path)) + '\n'
    found = section.search(text)
    if found is None:
        return text + '\n[profile.ci.junit]\n' + value
    body = re.sub(r'(?m)^path\s*=.*\n?', '', found[1])
    return text[:found.start()] + '[profile.ci.junit]\n' + value + body + text[found.end():]


def no_publish(_run: Path) -> None:
    pass


def measure_machine(folder: Path, machine: str, source: Path, environment: Mapping[str, str], *,
                    prefix: Sequence[str] = ('nix', 'develop', '--no-write-lock-file', '.#default', '-c'),
                    vulkaninfo: str = 'vulkaninfo',
                    publish: Callable[[Path], None] = no_publish, host: bool = True) -> None:
    if host:
        folder = host_trial_folder(folder)
    destination = folder / 'machines' / machine
    destination.mkdir(parents=True, exist_ok=False)
    payload = checked([*prefix, vulkaninfo, '--summary'], source, environment=environment)
    adapters = hardware_vulkan(payload)
    _ = (destination / 'vulkan.txt').write_text(payload + '\n')
    write_json(destination / 'machine.json', {'adapters': adapters, 'commit': read_json(folder / 'request.json')['commit'],
                                            'archive_sha256': digest(folder / 'hana-tests.tar.zst')})
    for number in (1, 2):
        run = destination / str(number)
        run.mkdir()
        try:
            run_source = run / 'source'
            _ = shutil.copytree(source, run_source, ignore=shutil.ignore_patterns('target', '.git'))
            config = run / 'nextest.toml'
            text = (source / '.config/nextest.toml').read_text()
            # Keep all existing groups, priorities, and timeouts.
            text = junit_config(text, run / 'junit.xml')
            _ = config.write_text(text)
            wrapper = shlex.join([sys.executable, str(folder / 'gpu_trial_wrapper.py')])
            run_environment = {**environment, 'GPU_TRIAL_SAMPLES': str(run / 'samples'),
                               'CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUNNER': wrapper,
                               'CARGO_TARGET_DIR': str(run / 'cargo-target')}
            # Nextest requires the explicit extraction directory to exist.
            (run / 'archive').mkdir()
            command = [*prefix, environment.get('GPU_TRIAL_CARGO', 'cargo'), 'nextest', 'run', *archive_arguments(folder, run_source),
                       '--extract-to', str(run / 'archive'), '--config-file', str(config), '--retries', '0', '--no-fail-fast']
            with (run / 'nextest.log').open('wb') as output:
                status = subprocess.run(command, cwd=run_source, env=run_environment,
                                        stdout=output, stderr=subprocess.STDOUT, check=False).returncode
            write_json(run / 'run.json', {'status': status, 'config_sha256': digest(config)})
            publish(run)
            if status not in (0, 100):
                raise ValueError(f'Nextest could not complete run {number}; exit {status}')
        finally:
            if host:
                _ = host_trial_folder(folder)
            for name in ('archive', 'source', 'cargo-target'):
                remove_path(run / name)


def prepare_worker(folder: Path) -> None:
    with held_trial(folder):
        prepare_worker_locked(folder)


def prepare_worker_locked(folder: Path) -> None:
    folder = host_trial_folder(folder)
    request = read_json(folder / 'request.json')
    repo, commit = Path(str(request['repo'])), str(request['commit'])
    tools = {name: str(path) for name, path in mapping(request['tools']).items()}
    environment = trial_environment()
    environment.update(GPU_TRIAL_NIX=tools['nix'], GPU_TRIAL_NIX_STORE=tools['nix-store'])
    environment['PATH'] = os.pathsep.join(dict.fromkeys(
        [str(Path(path).parent) for name, path in tools.items() if name != 'vulkan_tools']
        + [environment.get('PATH', '')]))
    source = folder / 'source'
    mode = nextest_request(request)
    toolchain = nix_toolchain(folder, source, environment, hana_nextest=isinstance(mode, HanaGpuNextestRequest))
    environment.update(GPU_TRIAL_CARGO=toolchain + '/bin/cargo', CARGO=toolchain + '/bin/cargo',
                       RUSTC=toolchain + '/bin/rustc', RUSTDOC=toolchain + '/bin/rustdoc', PATH=toolchain + '/bin:' + environment.get('PATH', ''))
    if isinstance(mode, HanaGpuNextestRequest):
        version = checked([toolchain + '/bin/cargo-nextest', 'nextest', '--version'], source, environment=environment)
        verify_hana_nextest(source, version)
    else:
        version = ''
    cache_dependencies(folder, source, environment)
    environment['CARGO_HOME'] = str(folder / 'cargo-home')
    prefix = [tools['nix'], 'develop', '--no-write-lock-file', '.#default', '-c']
    inputs = ['closure.export', 'source.tar', 'runtime.env', 'gpu-trial.nix']
    if isinstance(mode, HanaGpuNextestRequest):
        system = build_system(folder, source, environment, toolchain, hana_nextest=True)
    else:
        # Require a hardware GPU before the expensive archive build.
        _ = hardware_vulkan(checked([*prefix, tools['vulkaninfo'], '--summary'], source, environment=environment))
        tests = archive_tests(folder, source, environment, prefix)
        write_json(folder / 'inventory.json', {'tests': tests})
        _ = shutil.copyfile(WRAPPER, folder / WRAPPER.name)
        system = build_system(folder, source, environment, toolchain)
        inputs += ['hana-tests.tar.zst', 'gpu_trial_wrapper.py', 'inventory.json']
        version = checked([*prefix, environment['GPU_TRIAL_CARGO'], 'nextest', '--version'], source, environment=environment)
    manifest: dict[str, object] = {'schema': 2, 'toolchain_source': 'nix', 'trial': folder.name, 'commit': commit,
        'tree': checked([tools['git'], 'rev-parse', commit + '^{tree}'], repo, environment=environment),
        'created': datetime.now(UTC).isoformat(), 'system': system, 'toolchain': toolchain,
        'rustc': checked([*prefix, environment['RUSTC'], '-Vv'], source, environment=environment),
        'nextest': version, 'limits': request['limits'], 'state': 'preparing',
        'sha256': {name: digest(folder / name) for name in inputs},
        'locks': {name: digest(source / name) for name in ('Cargo.lock', 'flake.lock')}}
    if isinstance(mode, HanaGpuNextestRequest):
        manifest['hana_nextest'] = asdict(mode)
    else:
        manifest['vulkan_tools'] = tools['vulkan_tools']
    write_json(folder / 'manifest.json', manifest)
    if isinstance(mode, GpuSizeComparison):
        _ = host_trial_folder(folder)
        remove_path(source)
        source.mkdir()
        with tarfile.open(folder / 'source.tar') as archive:
            archive.extractall(source, filter='data')
        remove_path(folder / 'archive-nextest.toml')
        remove_path(folder / 'toolchain-probe-home')
        measure_machine(folder, 'natedev', source, environment, prefix=prefix, vulkaninfo=tools['vulkaninfo'])
    manifest['state'] = 'prepared'
    write_json(folder / 'manifest.json', manifest)
    _ = host_trial_folder(folder)
    for path in folder.iterdir():
        if path.name not in PREPARED_NAMES:
            remove_path(path)


@dataclass(frozen=True)
class TestSample:
    status: int
    wall_s: float
    largest_process_peak_rss_kib: float


@dataclass(frozen=True)
class IncompleteTest:
    reason: str


@dataclass(frozen=True)
class MachineTestPair:
    first: TestSample | IncompleteTest
    second: TestSample | IncompleteTest

    def verdict(self) -> str:
        if isinstance(self.first, IncompleteTest) or isinstance(self.second, IncompleteTest):
            return 'incomplete'
        return 'flaky' if self.first.status != self.second.status else 'stable'


def sample(row: dict[str, object]) -> TestSample:
    if type(row['status']) is not int:
        raise ValueError('The test status must be an integer')
    values: list[float] = []
    for name in ('wall_s', 'largest_process_peak_rss_kib'):
        value = row[name]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError('The test has an invalid ' + name)
        values.append(float(value))
    return TestSample(row['status'], values[0], values[1])


def comparison(baseline: MachineTestPair, candidate: MachineTestPair) -> str:
    if baseline.verdict() == 'flaky':
        return 'inconclusive'
    if baseline.verdict() == 'incomplete' or candidate.verdict() == 'incomplete':
        return 'incomplete'
    if candidate.verdict() == 'flaky':
        return 'flaky'
    assert isinstance(baseline.first, TestSample) and isinstance(candidate.first, TestSample)
    return 'same' if baseline.first.status == candidate.first.status else 'difference'


def read_run(folder: Path) -> dict[tuple[str, str], TestSample]:
    rows: dict[tuple[str, str], TestSample] = {}
    try:
        status = read_json(folder / 'run.json')['status']
        if type(status) is not int or status not in (0, 100) or not (folder / 'junit.xml').is_file():
            return rows
        for path in sorted((folder / 'samples').glob('*.json')):
            row = read_json(path)
            key = str(row['binary']), str(row['test'])
            if key in rows:
                raise ValueError('The run contains a duplicate test')
            rows[key] = sample(row)
    except (OSError, KeyError, ValueError):
        return {}
    return rows


def sample_columns(item: TestSample | IncompleteTest) -> tuple[str, str, str]:
    if isinstance(item, IncompleteTest):
        return 'missing', 'missing', 'missing'
    return str(item.status), str(item.wall_s), str(item.largest_process_peak_rss_kib)


def compare(folder: Path) -> str:
    manifest = read_json(folder / 'manifest.json')
    expected = read_json(folder / 'inventory.json')['tests']
    if not isinstance(expected, list):
        raise ValueError('The trial inventory must be a list')
    machines: dict[str, tuple[dict[tuple[str, str], TestSample], dict[tuple[str, str], TestSample]]] = {}
    hashes = mapping(manifest['sha256'])
    if digest(folder / 'inventory.json') != hashes['inventory.json']:
        raise ValueError('The inventory differs from the manifest')
    for machine in sorted((folder / 'machines').glob('*')):
        if not machine.is_dir():
            continue
        try:
            identity = read_json(machine / 'machine.json')
            valid = identity.get('commit') == manifest['commit'] and identity.get('archive_sha256') == hashes['hana-tests.tar.zst']
            _ = hardware_vulkan((machine / 'vulkan.txt').read_text())
        except (OSError, ValueError):
            valid = False
        machines[machine.name] = ((read_run(machine / '1'), read_run(machine / '2')) if valid else ({}, {}))
    missing = IncompleteTest('No completed test measurement')
    baseline = machines.get('natedev', ({}, {}))
    _ = machines.setdefault('natedev', ({}, {}))
    for name in mapping(mapping(manifest['limits'])['spot_max_usd_per_hour']):
        _ = machines.setdefault(name, ({}, {}))
    lines = ['# GPU size trial', '', 'Peak RSS means the kernel peak RSS of the largest single process of the test.',
             '', '| Binary | Test | Machine | Status 1 / 2 | Wall seconds 1 / 2 | Peak RSS KiB 1 / 2 | Result |',
             '|---|---|---|---|---|---|---|']
    for raw in cast(list[object], expected):
        test = mapping(raw)
        key = str(test['binary']), str(test['test'])
        base_pair = MachineTestPair(*(run.get(key, missing) for run in baseline))
        for name, runs in machines.items():
            pair = MachineTestPair(*(run.get(key, missing) for run in runs))
            verdict = pair.verdict() if name == 'natedev' else comparison(base_pair, pair)
            first, second = sample_columns(pair.first), sample_columns(pair.second)
            values = [value for pair_values in zip(first, second) for value in pair_values]
            escaped = [value.replace('|', '\\|').replace('\n', ' ') for value in (key[0], key[1], name)]
            lines.append('| ' + ' | '.join([*escaped, *(values[index] + ' / ' + values[index + 1]
                                                        for index in (0, 2, 4)), verdict]) + ' |')
    report = '\n'.join(lines) + '\n'
    _ = (folder / 'comparison.md').write_text(report)
    return report


def validate_prepared(folder: Path) -> dict[str, object]:
    manifest = read_json(folder / 'manifest.json')
    mode = nextest_request(manifest)
    if isinstance(mode, HanaGpuNextestRequest):
        if any(not re.fullmatch(r'[0-9a-f]{40}', str(manifest[key])) for key in ('commit', 'tree')):
            raise ValueError('The prepared Hana source identity is invalid')
    if manifest.get('schema') != 2 or manifest.get('toolchain_source') != 'nix':
        raise ValueError('Prepare the trial again with the Nix toolchain and current GPU system module')
    if manifest.get('state') != 'prepared' or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(manifest['trial'])):
        raise ValueError('The trial folder has no completed preparation')
    removed = (folder / 'inputs-removed.json').exists()
    uploaded = removed and (folder / 'online.json').exists() and read_json(folder / 'online.json').get('uploaded') is True
    for name, checksum in mapping(manifest['sha256']).items():
        if Path(name).name != name:
            raise ValueError('The manifest contains an invalid artifact name')
        if removed and name in RUN_INPUT_NAMES and not (folder / name).exists():
            if not uploaded:
                raise ValueError('The trial inputs were removed before upload; prepare another trial')
            continue
        if digest(folder / name) != checksum:
            raise ValueError('The prepared artifact differs from the manifest: ' + name)
    return manifest


def gpu_service_name(trial: str) -> str:
    return 'gpu-size-trial-' + hashlib.sha256(trial.encode()).hexdigest()[:24]


def confirm_assigned_gpu_service(trial: str) -> None:
    """Retain the exact live claim before the service can launch a box."""
    from scripts.lib import process_start
    from scripts.mac_test import claim_state

    paths = claim_state.StatePaths(ssh_target_state_directory('hana-gpu'), 'hana-gpu', platform='linux')
    with claim_state.locked(paths.lock):
        for held in claim_state.read_claims(paths):
            record = held.record
            if record['claim_id'] != trial or 'remote_host' not in record:
                continue
            if (record.get('cleanup_pending')
                    or process_start.process_start(record['pid']) != process_start.Started(record['proc_start'])):
                break
            cloud_gate.save(held.path, dict(record, cleanup_pending=True))
            return
    raise ValueError('The Hana GPU service has no live claim')


def assigned_gpu_cleanup_pending(trial: str) -> bool:
    from scripts.mac_test import claim_state

    paths = claim_state.StatePaths(ssh_target_state_directory('hana-gpu'), 'hana-gpu', platform='linux')
    with claim_state.locked(paths.lock):
        return any(held.record['claim_id'] == trial and held.record.get('cleanup_pending')
                   for held in claim_state.read_claims(paths))


def assigned_gpu_cleanup_confirmed(trial: str) -> bool:
    """Read the exact trial's cleanup evidence without waiting for the worker."""
    if not re.fullmatch(r'[0-9a-f]{32}', trial):
        return False
    try:
        if read_json(cleanup_receipt(trial_root() / trial)) == {'trial': trial}:
            return True
    except (OSError, ValueError):
        pass

    directory = ssh_target_state_directory('aws')
    try:
        with (directory / 'gpu-size-trial/controller.lock').open('rb') as lock:
            # The worker holds this lock across regional retries.
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            rows = [row for row in ledger.CostLedger(directory).read_snapshot().gate_reservations
                    if isinstance(row.trial, ledger.GateTrialBudget) and row.trial.trial_id == trial]
            return bool(rows) and all(not ledger.gate_is_open(row) for row in rows)
    except (OSError, ledger.LedgerUnavailable):
        return False


def stop_assigned_gpu_service(trial: str) -> int:
    if not assigned_gpu_cleanup_pending(trial) or assigned_gpu_cleanup_confirmed(trial):
        return 0
    return subprocess.run(['systemctl', '--user', 'stop', gpu_service_name(trial)]).returncode


def start_service(folder: Path, directory: Path, settings_path: Path,
                  execute: Callable[[Sequence[str]], int], *, assigned: bool = False) -> int:
    folder = host_trial_folder(folder)
    with held_trial(folder):
        manifest = validate_prepared(folder)
        trial = ledger.text(manifest, 'trial')
        arguments = ['--worker', str(folder), '--directory', str(directory), '--settings', str(settings_path)]
        if assigned:
            if trial != folder.name:
                raise ValueError('The Hana GPU service does not match its assigned trial')
            arguments.append('--assigned')
        command = cloud_gate.service_command('scripts.aws_target.gpu_size_trial', arguments, gpu_service_name(trial))
        if assigned and 'BUILD_TARGETS_STATE_DIR' in os.environ:
            command.insert(1, '--setenv=BUILD_TARGETS_STATE_DIR=' + os.environ['BUILD_TARGETS_STATE_DIR'])
        if isinstance(nextest_request(manifest), HanaGpuNextestRequest):
            command = ['--working-directory=' + str(ROOT) if arg.startswith('--working-directory=') else arg
                       for arg in command]
        return execute(command)


def run_hana_nextest(root: Path, source: Path, manifest: dict[str, object], links: dict[str, object],
                     environment: dict[str, str]) -> dict[str, object]:
    _ = hardware_vulkan(checked(['vulkaninfo', '--summary'], source, environment=environment))
    arguments = string_list(mapping(manifest['hana_nextest'])['arguments'])
    build_options = arguments[:arguments.index('--')] if '--' in arguments else arguments
    locked = [] if '--locked' in build_options or '--frozen' in build_options else ['--locked']
    offline = [] if '--offline' in build_options or '--frozen' in build_options else ['--offline']
    target = root / 'target'
    target.mkdir()
    environment.update(CARGO_TARGET_DIR=str(target), CARGO_BUILD_JOBS='8', NEXTEST_EXPERIMENTAL_LIBTEST_JSON='1')
    command = [environment.get('GPU_TRIAL_CARGO', 'cargo'), 'nextest', 'run', *locked, *offline,
               '--target-dir', str(target), '--no-tests', 'fail', '--color', 'never',
               '--message-format', 'libtest-json', '--message-format-version', '0.1', *arguments]
    with (root / 'stdout').open('w+b') as stdout, (root / 'stderr').open('w+b') as stderr:
        status = subprocess.run(command, cwd=source, env=environment, stdout=stdout, stderr=stderr).returncode
        if status < 0:
            raise ValueError(f'Cargo nextest stopped on signal {-status}')
        if status == 4:
            raise ValueError('Cargo nextest selected no tests')
        _ = stdout.seek(0)
        _ = stderr.seek(0)
        return {'attempt': links['attempt'], 'commit': manifest['commit'], 'tree': manifest['tree'],
                'exit_code': status, 'stdout': stdout.read().decode('utf-8', errors='replace'),
                'stderr': stderr.read().decode('utf-8', errors='replace')}


GUEST = r'''
root = Path('/var/lib/gpu-size-trial')
os.chdir(root)
links = read_json(root / 'links.json')
manifest = read_json(root / 'manifest.json')


def transfer(operation, *arguments):
    try:
        subprocess.run(['curl', '--fail', '--silent', *arguments], check=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(operation + ': ' + type(error).__name__) from None


def publish(run):
    machine = run.parent
    bundle = root / ('result-' + run.name + '.tar.gz')
    with tarfile.open(bundle, 'w:gz') as output:
        for name in ('machine.json', 'vulkan.txt'):
            output.add(machine / name, arcname='machines/' + machine.name + '/' + name)
        for name in ('samples', 'run.json', 'junit.xml', 'nextest.log', 'nextest.toml'):
            path = run / name
            if path.exists():
                output.add(path, arcname='machines/' + machine.name + '/' + run.name + '/' + name)
    transfer('Upload GPU run result', '--max-time', '60', '-T', str(bundle), str(links['result-' + run.name]))


status = 'incomplete'
reason = ''
result = {}
try:
    setup = read_json(root / 'setup.json')
    setup['runner_start'] = time.time()
    setup['boot_seconds'] = setup['boot'] - float(links['started'])
    setup['import_seconds'] = setup['import_end'] - setup['import_start']
    setup['reboot_seconds'] = setup['runner_start'] - setup['reboot']
    setup['setup_seconds'] = setup['runner_start'] - float(links['started'])
    write_json(root / 'setup.json', setup)
    if 'hana_nextest' not in manifest:
        transfer('Download GPU archive', '--max-time', '120', str(links['archive']),
                 '-o', str(root / 'hana-tests.tar.zst'))
    for name, checksum in mapping(manifest['sha256']).items():
        if Path(name).name != name or digest(root / name) != checksum:
            raise ValueError('The artifact differs from the manifest: ' + name)
    source = root / 'source'
    source.mkdir()
    with tarfile.open(root / 'source.tar') as archive:
        archive.extractall(source, filter='data')
    version = subprocess.check_output(['cargo', 'nextest', '--version'], text=True).strip()
    if version != manifest['nextest']:
        raise ValueError('The box nextest version differs from the prepared version')
    environment = trial_environment()
    environment['CARGO_HOME'] = str(root / 'cargo-home')
    if 'hana_nextest' in manifest:
        verify_hana_nextest(source, version)
        result['nextest'] = run_hana_nextest(root, source, manifest, links, environment)
    else:
        measure_machine(root, str(links['instance_type']), source, environment, prefix=(), publish=publish, host=False)
    status = 'complete'
except Exception as error:
    reason = str(error)
finally:
    finished = root / 'finished.json'
    write_json(finished, {'status': status, 'reason': reason,
                         'setup': read_json(root / 'setup.json'), 'finished': time.time(), **result})
    transfer('Upload GPU final result', '--max-time', '60', '-T', str(finished), str(links['finished']))
'''


def guest_program() -> str:
    functions = (mapping, read_json, write_json, digest, checked, hardware_vulkan, trial_environment, remove_path,
                 archive_arguments, junit_config, no_publish, measure_machine, string_list, verify_hana_nextest, run_hana_nextest)
    return ('from __future__ import annotations\n'
            + 'from pathlib import Path\nfrom collections.abc import Callable, Mapping, Sequence\n'
            + 'from typing import cast\nimport hashlib, json, os, re, shlex, shutil, subprocess, sys, tarfile, time\n'
            + '\n'.join(inspect.getsource(function) for function in functions) + GUEST)


def gpu_user_data(links: dict[str, str], system: str, toolchain: str, expires: int, stop_at: int) -> str:
    """Import the prepared system and reboot once before the unit starts."""
    if not re.fullmatch(r'/nix/store/[A-Za-z0-9+._?-]+', system) or not re.fullmatch(r'/nix/store/[A-Za-z0-9+._?-]+', toolchain):
        raise ValueError('The prepared system or toolchain path is invalid')
    script = f'''#!/bin/sh
set -eu
export PATH=/run/current-system/sw/bin:$PATH
if [ "$(date +%s)" -ge {stop_at} ]; then
    /run/current-system/sw/bin/shutdown -h now
    exit 0
fi
systemd-run --unit=gpu-trial-bootstrap-expiry --on-calendar=@{stop_at} --timer-property=AccuracySec=1s /run/current-system/sw/bin/shutdown -h now
root=/var/lib/gpu-size-trial
# Amazon init can read this script again after the reboot.
[ ! -e "$root/imported" ] || exit 0
mkdir -p "$root"
exec >> "$root/boot.log" 2>&1
boot=$(date +%s)
curl --fail --silent --max-time {max(1, stop_at - int(links['started']))} {shlex.quote(links['payload'])} -o "$root/payload.tar" 2>/dev/null ||
    {{ echo "Download GPU payload failed"; exit 1; }}
echo {shlex.quote(links['payload_sha256'] + '  /var/lib/gpu-size-trial/payload.tar')} | sha256sum -c -
tar -xf "$root/payload.tar" -C "$root"
curl --fail --silent --max-time {max(1, stop_at - int(links['started']))} {shlex.quote(links['closure'])} -o "$root/closure.export" 2>/dev/null ||
    {{ echo "Download GPU closure failed"; exit 1; }}
echo {shlex.quote(links['closure_sha256'] + '  /var/lib/gpu-size-trial/closure.export')} | sha256sum -c -
import_start=$(date +%s)
nix-store --import < "$root/closure.export"
import_end=$(date +%s)
cat > "$root/links.json" <<'GPU_LINKS'
{json.dumps(links)}
GPU_LINKS
printf '{{"boot":%s,"import_start":%s,"import_end":%s,"reboot":%s}}\\n' "$boot" "$import_start" "$import_end" "$(date +%s)" > "$root/setup.json"
cat > "$root/run" <<'GPU_RUN'
#!/run/current-system/sw/bin/bash
set -euo pipefail
cd /var/lib/gpu-size-trial
. ./runtime.env
export PATH={shlex.quote(toolchain + '/bin')}:/run/current-system/sw/bin:$PATH
export HOME=/home/gpu-trial CARGO_HOME=/var/lib/gpu-size-trial/cargo-home
export TMPDIR=/var/lib/gpu-size-trial/tmp TMP=/var/lib/gpu-size-trial/tmp TEMP=/var/lib/gpu-size-trial/tmp
export XDG_CACHE_HOME=/var/lib/gpu-size-trial/cache XDG_RUNTIME_DIR=/var/lib/gpu-size-trial/runtime
mkdir -p "$TMPDIR" "$XDG_CACHE_HOME" "$XDG_RUNTIME_DIR"
export RUSTC={shlex.quote(toolchain + '/bin/rustc')} RUSTDOC={shlex.quote(toolchain + '/bin/rustdoc')}
export GPU_TRIAL_CARGO={shlex.quote(toolchain + '/bin/cargo')}
exec /run/current-system/sw/bin/python3 ./guest.py
GPU_RUN
chmod 700 "$root/run"
{shlex.quote(system + '/bin/switch-to-configuration')} boot
printf '%s\\n' {stop_at} > "$root/stop-at"
touch "$root/imported"
reboot
'''
    if len(script.encode()) > 16384 or stop_at > expires:
        raise ValueError('The GPU bootstrap exceeds its size or time limit')
    return base64.b64encode(script.encode()).decode()


def gpu_aws_executor(execute: AwsExecutor) -> AwsExecutor:
    """Keep AWS arguments and response details out of trial diagnostics."""
    def call(arguments: Sequence[str], *, capture_output: bool, text: bool,
             timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        operation = arguments[6]
        environment = {**(os.environ if env is None else env), 'AWS_CLI_ERROR_FORMAT': 'legacy'}
        if operation == 'run-instances':
            environment['AWS_MAX_ATTEMPTS'] = '1'
        try:
            result = execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=environment)
        except (OSError, subprocess.SubprocessError) as error:
            raise AwsUnavailable(f'{operation}: {type(error).__name__}') from None
        if result.returncode:
            error = AwsUnavailable(result.stderr.strip())
            expected = ''.join(part.capitalize() for part in operation.split('-'))
            code = aws_error_code(error, allow_status=True, operation=expected)
            if not code:
                raise AwsUnavailable(f'{operation}: AwsUnavailable')
            first_attempt = operation == 'run-instances' and bool(aws_error_code(error, first_attempt_of='RunInstances'))
            raise cloud_gate.AwsServiceFailure(operation, code, first_attempt=first_attempt)
        return result
    return call


@dataclass(frozen=True)
class PricedSubnet:
    price: Decimal
    subnet: dict[str, object]


@dataclass(frozen=True)
class SelectedHanaGpuOffer:
    region: str
    instance_type: str
    zone: str
    estimated_total_usd: Decimal


@dataclass(frozen=True)
class NoEligibleHanaGpuOffer:
    reason: str = 'No Hana GPU Spot offer fits the applied quota and price cap after current use'


def gpu_box_extra_cost(gate_settings: conf.ConfigRead, account: conf.ConfigRead, region: str, *,
                       root_gib: int, booked_seconds: int, poll_seconds: int, keys_per_check: int) -> Decimal:
    polls = (booked_seconds + poll_seconds - 1) // poll_seconds
    # Include the final check.
    list_requests = (polls + 1) * keys_per_check
    list_cost = list_requests * conf.decimal(gate_settings.text('gpu_size_trial_s3_list_usd_per_1000')) / 1000
    booked_hours = Decimal(booked_seconds) / 3600
    regional_gp3 = run_cost.region_numbers(gate_settings, 'gpu_size_trial_gp3_usd_per_gib_month_by_region')
    if region in regional_gp3:
        gp3_price = regional_gp3[region]
    else:
        gp3_price = conf.decimal(conf.read_or_repository('hana_aws').text('gp3_usd_per_gib_month'))
    disk_cost = root_gib * gp3_price * booked_hours / (30 * 24)
    ipv4_cost = conf.decimal(account.text('ipv4_usd_per_month')) * booked_hours / 730
    return list_cost + disk_cost + ipv4_cost


def gpu_box_extra_refusal(cost: Decimal, allowance: Decimal) -> str:
    return f'GPU trial LIST, root disk, and IPv4 cost ${cost:.5f} exceeds the ${allowance} allowance'


def gpu_attempt_id(trial_id: str, size: str, region: str = '') -> str:
    parts = (trial_id, size, region) if region else (trial_id, size)
    return hashlib.sha256(':'.join(parts).encode()).hexdigest()


def gpu_box(folder: Path, trial_id: str, size: str, attempt: str) -> Path:
    box = folder / 'boxes' / size
    if attempt == gpu_attempt_id(trial_id, size):
        return box
    if not re.fullmatch(r'[a-f0-9]{64}', attempt):
        raise ValueError('The GPU attempt ID is invalid')
    return box / attempt


@final
class GpuTrialWorker:
    """Run sequential boxes with durable bookings and shared gate cleanup."""
    def __init__(self, gates: cloud_gate.GateWorker) -> None:
        self.gates = gates

    def select_region(self, deployment: dict[str, object], limits: TrialLimits) -> dict[str, object]:
        values = conf.read_or_repository('cloud_gate')
        needed = required_vcpus(limits)
        regions = {str(row['region']): row for row in ledger.rows(deployment, 'regions')}
        applied: list[str] = []
        for region in values.words('spot_candidate_regions'):
            reply = self.gates.regional_api(region).call('get-service-quota', '--service-code', 'ec2',
                '--quota-code', 'L-3819A6DF', service='service-quotas')
            quota = conf.decimal(str(mapping(reply['Quota'])['Value']))
            applied.append(f'{region}={quota.normalize():f}')
            if quota >= needed:
                if region not in regions:
                    raise ValueError('The granted GPU region has no gate deployment: ' + region)
                return regions[region]
        raise ValueError(f'No configured region has an applied G and VT Spot quota of at least {needed} vCPUs; '
                         + 'applied vCPUs: ' + ', '.join(applied))

    def available_gpu_vcpus(self, region: str, needed: int) -> Decimal:
        api = self.gates.regional_api(region)
        quota = spot_quota(api, 'L-3819A6DF')
        if quota < needed:
            return Decimal(0)
        # A one-time RunInstances request launches or fails immediately and leaves no open request.
        instances = api.call('describe-instances', '--filters', 'Name=instance-lifecycle,Values=spot',
                             'Name=instance-state-name,Values=pending,running',
                             'Name=instance-type,Values=g*.*,vt*.*')
        if instances.get('NextToken'):
            raise ValueError('The regional GPU Spot usage is incomplete')

        sizes: dict[str, str] = {}
        for reservation in ledger.rows(instances, 'Reservations'):
            for instance in ledger.rows(reservation, 'Instances'):
                sizes[ledger.text(instance, 'InstanceId')] = ledger.text(instance, 'InstanceType')
        used = 0
        for size in sizes.values():
            vcpus = largest_gate_vcpus((size,))
            if not vcpus:
                raise ValueError('The regional GPU Spot usage has a type with an unknown vCPU count')
            used += vcpus
        return max(Decimal(0), quota - used)

    def require_no_open_booking(self, *, current_attempt: str = '') -> None:
        if any(row.attempt != current_attempt and ledger.gate_is_open(row)
               for row in self.gates.book.read_snapshot().gate_reservations):
            raise ValueError('An earlier AWS gate booking remains open. Complete cleanup for that run, then retry.')

    def select_nextest_offer(self, deployment: dict[str, object], prices: dict[str, Decimal], *,
                             lifetime_minutes: int, root_gib: int, extra_allowance: Decimal,
                             excluded_regions: Sequence[str] = ()) -> SelectedHanaGpuOffer | NoEligibleHanaGpuOffer:
        if not prices:
            return NoEligibleHanaGpuOffer()
        regions = {str(row['region']): row for row in ledger.rows(deployment, 'regions')}
        offers: list[SelectedHanaGpuOffer] = []
        refused_costs: list[Decimal] = []
        gate_settings = cloud_gate.settings()
        account = conf.read(self.gates.settings_path)
        poll_seconds = gate_settings.whole('gpu_size_trial_result_poll_s')
        booked_seconds = lifetime_minutes * 60 + HANA_EXPIRY_MARGIN_SECONDS
        booked_hours = Decimal(booked_seconds) / 3600
        smallest = min(largest_gate_vcpus((size,)) for size in prices)
        for region in gate_settings.words('spot_candidate_regions'):
            if region not in regions or region in excluded_regions:
                continue
            regional = regions[region]
            quota = self.available_gpu_vcpus(region, smallest)
            if quota < smallest:
                continue
            extra_cost = gpu_box_extra_cost(gate_settings, account, region, root_gib=root_gib,
                                           booked_seconds=booked_seconds, poll_seconds=poll_seconds, keys_per_check=1)
            for size, cap in prices.items():
                if largest_gate_vcpus((size,)) > quota:
                    continue
                subnets = self.offered_zones(regional, size)
                if subnets:
                    offer = self.price_zone(regional, size, cap, subnets)
                    if isinstance(offer, PricedSubnet):
                        if extra_cost > extra_allowance:
                            refused_costs.append(extra_cost)
                            continue
                        total = offer.price * booked_hours + extra_cost
                        offers.append(SelectedHanaGpuOffer(region, size, ledger.text(offer.subnet, 'zone'), total))
        if offers:
            return min(offers, key=lambda offer: offer.estimated_total_usd)
        if refused_costs:
            return NoEligibleHanaGpuOffer(gpu_box_extra_refusal(min(refused_costs), extra_allowance))
        return NoEligibleHanaGpuOffer()

    def offered_zones(self, regional: dict[str, object], size: str) -> list[dict[str, object]]:
        api = self.gates.regional_api(str(regional['region']))
        offered: set[str] = set()
        token = ''
        while True:
            arguments = ['--location-type', 'availability-zone', '--filters', 'Name=instance-type,Values=' + size,
                         '--no-paginate']
            if token:
                arguments += ['--next-token', token]
            reply = api.call('describe-instance-type-offerings', *arguments)
            offered.update(str(row['Location']) for row in ledger.rows(reply, 'InstanceTypeOfferings')
                           if row['InstanceType'] == size)
            token = str(reply.get('NextToken', ''))
            if not token:
                break
        return [row for row in ledger.rows(regional, 'subnets') if row['zone'] in offered]

    def price_zone(self, regional: dict[str, object], size: str, maximum: Decimal,
                   subnets: Sequence[dict[str, object]]) -> PricedSubnet | str:
        api = self.gates.regional_api(str(regional['region']))
        reply = api.call('describe-spot-price-history', '--instance-types', size, '--product-descriptions', 'Linux/UNIX',
                         '--start-time', datetime.fromtimestamp(self.gates.clock(), UTC).isoformat())
        prices = ledger.regional_spot_prices(api.region, reply, (size,), int(self.gates.clock()))
        available = [(price.usd_per_hour, subnet) for subnet in subnets for price in prices
                     if price.zone == subnet['zone'] and price.instance_type == size and price.usd_per_hour <= maximum]
        if not available:
            return 'price above max' if prices else 'price unavailable'
        price, subnet = min(available, key=lambda item: item[0])
        return PricedSubnet(price, subnet)

    def upload(self, bucket: str, key: str, path: Path, timeout_s: int) -> None:
        if path.stat().st_size < 5 * 1024**3:
            _ = self.gates.api.call('put-object', '--bucket', bucket, '--key', key, '--body', str(path),
                service='s3api', timeout_s=timeout_s)
        else:
            _ = self.gates.api.call('cp', str(path), 's3://' + bucket + '/' + key, '--only-show-errors', service='s3', timeout_s=timeout_s)

    def artifacts(self, folder: Path, online: dict[str, object]) -> None:
        folder = host_trial_folder(folder)
        if online.get('uploaded'):
            return
        payload = folder / 'payload.tar'
        _ = (folder / 'guest.py').write_text(guest_program())
        payload_names = ['source.tar', 'cargo-home', 'runtime.env', 'gpu-trial.nix', 'closure.json', 'request.json',
                         'manifest.json', 'guest.py']
        upload_names = ['payload.tar', 'closure.export', 'manifest.json']
        if isinstance(nextest_request(read_json(folder / 'manifest.json')), GpuSizeComparison):
            payload_names += ['inventory.json', 'gpu_trial_wrapper.py']
            upload_names.insert(2, 'hana-tests.tar.zst')
        try:
            with tarfile.open(payload, 'w') as archive:
                for name in payload_names:
                    archive.add(folder / name, arcname=name)
            prefix = 'requests/gpu-size-trial/' + str(online['trial'])
            for name in upload_names:
                self.upload(str(online['bucket']), prefix + '/' + name, folder / name,
                            ledger.instant(online, 'lifetime_minutes') * 60)
            online['uploaded'] = True
            online['payload_sha256'] = digest(payload)
            cloud_gate.save(folder / 'online.json', {key: value for key, value in online.items() if key != 'bucket'})
        finally:
            _ = host_trial_folder(folder)
            remove_path(payload)

    def signed_links(self, folder: Path, online: dict[str, object], request: dict[str, object]) -> dict[str, str]:
        reply = self.gates.api.call('export-credentials', '--format', 'process', service='configure')
        signing = s3_presign.SigningKey(str(reply['AccessKeyId']), str(reply['SecretAccessKey']), str(reply.get('SessionToken', '')))
        now = int(self.gates.clock())
        home = self.gates.api.region
        prefix = 'requests/gpu-size-trial/' + str(online['trial'])
        def url(method: str, key: str) -> str:
            return s3_presign.presign(method, str(online['bucket']) + '.s3.' + home + '.amazonaws.com', key,
                                    home, signing, datetime.fromtimestamp(now, UTC), max(1, int(str(request['expires'])) - now))
        result_prefix = 'artifacts/gpu-size-trial/' + str(request['attempt'])
        manifest = read_json(folder / 'manifest.json')
        hashes = mapping(manifest['sha256'])
        links = {'payload': url('GET', prefix + '/payload.tar'), 'closure': url('GET', prefix + '/closure.export'),
                 'finished': url('PUT', result_prefix + '/finished.json'),
                 'payload_sha256': str(online['payload_sha256']), 'closure_sha256': str(hashes['closure.export']),
                 'instance_type': str(request['instance_type']), 'started': str(request['started'])}
        if isinstance(nextest_request(manifest), HanaGpuNextestRequest):
            links['attempt'] = str(request['attempt'])
        else:
            links.update(archive=url('GET', prefix + '/hana-tests.tar.zst'),
                         archive_sha256=str(hashes['hana-tests.tar.zst']),
                         **{f'result-{number}': url('PUT', f'{result_prefix}/result-{number}.tar.gz')
                            for number in (1, 2)})
        return links

    def cleanup(self, folder: Path) -> bool:
        trial_id = str(read_json(folder / 'manifest.json')['trial'])
        attempts: list[cloud_gate.GateCleanupAttempt] = []
        for reservation in self.gates.book.read_snapshot().gate_reservations:
            if not isinstance(reservation.trial, ledger.GateTrialBudget) or reservation.trial.trial_id != trial_id:
                continue
            if not ledger.gate_is_open(reservation):
                continue
            box = gpu_box(folder, trial_id, reservation.instance_type, reservation.attempt)
            path = box / 'launch.json'
            recorded = path.is_file()
            attempts.append(cloud_gate.GateCleanupAttempt(reservation, read_json(path) if recorded else {},
                                                          not recorded and (box / 'request.json').is_file()))
        return cloud_gate.cleanup_attempts(attempts, self.gates)

    def download_result(self, bucket: str, key: str, path: Path) -> bool:
        # The exact key comes first in a general-purpose bucket's sorted response.
        reply = self.gates.api.call('list-objects-v2', '--bucket', bucket, '--prefix', key,
                                    '--max-keys', '1', '--no-paginate', service='s3api')
        return (any(row.get('Key') == key for row in ledger.rows({'Contents': reply.get('Contents', [])}, 'Contents'))
                and self.gates.download(bucket, key, path))

    def collect(self, folder: Path, box: Path, request: dict[str, object], online: dict[str, object],
                poll_seconds: int) -> None:
        prefix = 'artifacts/gpu-size-trial/' + str(request['attempt'])
        bucket = ledger.text(online, 'bucket')
        stop_at = ledger.instant(request, 'stop_at')
        while True:
            final_check = self.gates.clock() >= stop_at
            finished = self.download_result(bucket, prefix + '/finished.json', box / 'finished.json')
            for number in (1, 2):
                local = box / ('result-' + str(number) + '.tar.gz')
                temporary = local.with_suffix('.partial')
                if not local.exists() and self.download_result(bucket, prefix + '/' + local.name, temporary):
                    with tarfile.open(temporary) as archive:
                        # Results contain only this machine's report files.
                        allowed = 'machines/' + str(request['instance_type']) + '/'
                        if any(not member.name.startswith(allowed) or '..' in Path(member.name).parts
                               or not (member.isfile() or member.isdir()) for member in archive.getmembers()):
                            raise ValueError('The result archive contains an unexpected machine')
                        archive.extractall(folder, filter='data')
                    _ = temporary.replace(local)
            if finished or final_check:
                break
            remaining = stop_at - self.gates.clock()
            if remaining > 0:
                self.gates.pause(min(poll_seconds, remaining))
        if finished and read_json(box / 'finished.json').get('status') == 'complete' and not all(
                (box / ('result-' + str(number) + '.tar.gz')).is_file() for number in (1, 2)):
            raise ValueError('The completed box has missing result files')
        cloud_gate.save(box / 'result.json', read_json(box / 'finished.json') if finished
                        else {'status': 'incomplete', 'reason': 'The box reached its early stop time'})

    def collect_nextest(self, box: Path, request: dict[str, object], online: dict[str, object],
                        poll_seconds: int) -> None:
        attempt = ledger.text(request, 'attempt')
        key = f'artifacts/gpu-size-trial/{attempt}/finished.json'
        path = box / 'finished.json'
        bucket = ledger.text(online, 'bucket')
        stop_at = ledger.instant(request, 'stop_at')
        while self.gates.clock() < stop_at:
            if self.download_result(bucket, key, path):
                return
            remaining = stop_at - self.gates.clock()
            if remaining > 0:
                self.gates.pause(min(poll_seconds, remaining))
        if not self.download_result(bucket, key, path):
            raise ValueError('The Hana GPU box reached its stop time without a result')

    def execute(self, folder: Path, deployment: dict[str, object]) -> None:
        folder = host_trial_folder(folder)
        with held_trial(folder):
            try:
                self.run_trial(folder, deployment)
            except Exception as error:
                cloud_gate.save(folder / 'run-error.json', {'status': 'failed', 'reason': str(error)})
                raise
            finally:
                remove_run_inputs(folder, self.gates.clock())

    def run_trial(self, folder: Path, deployment: dict[str, object]) -> None:
        manifest = validate_prepared(folder)
        mode = nextest_request(manifest)
        allowed_types = string_list(deployment.get('gpu_instance_types', [])) if isinstance(mode, HanaGpuNextestRequest) else ()
        path = folder / 'online.json'
        online: dict[str, object]
        if path.exists():
            online = read_json(path)
            if online['trial'] != manifest['trial']:
                raise ValueError('The saved trial ID differs from the manifest')
            if online['deployment_id'] != deployment['deployment_id']:
                raise ValueError('The gate deployment changed. Prepare a new GPU trial.')
            if 'regional' in online:
                online['region'] = mapping(online.pop('regional'))['region']
                if 'selected_subnet' in online:
                    online['selected_zone'] = mapping(online.pop('selected_subnet'))['zone']
                if 'offerings' in online:
                    offerings = mapping(online['offerings'])
                    online['offerings'] = {size: [row['zone'] for row in ledger.rows(offerings, size)] for size in offerings}
                _ = online.pop('bucket', None)
                cloud_gate.save(path, online)
        else:
            if any(isinstance(row.trial, ledger.GateTrialBudget) and row.trial.trial_id == manifest['trial']
                   for row in self.gates.book.read_snapshot().gate_reservations):
                raise ValueError('The trial already has bookings; resume its original trial folder')
            root_gib = cloud_gate.settings().whole('gpu_size_trial_root_gib')
            if isinstance(mode, HanaGpuNextestRequest):
                self.require_no_open_booking()
                limits = hana_nextest_limits(mode)
                prices = {size: cap for size, cap in limits.spot_max_usd_per_hour.items() if size in allowed_types}
                if not prices:
                    raise ValueError('The gate deployment record allows none of the requested Hana GPU types. Update the gate setup.')
                candidate = self.select_nextest_offer(deployment, prices, lifetime_minutes=limits.lifetime_minutes,
                                                      root_gib=root_gib, extra_allowance=limits.box_extra_usd)
                if isinstance(candidate, NoEligibleHanaGpuOffer):
                    raise ValueError(candidate.reason)
                region = candidate.region
                selection: dict[str, object] = {'types': [candidate.instance_type], 'selected_zone': candidate.zone}
            else:
                limits = settings()
                regional = self.select_region(deployment, limits)
                region = regional['region']
                selection = {'types': list(limits.spot_max_usd_per_hour)}
            online = {'trial': manifest['trial'], 'region': region,
                      'deployment_id': deployment['deployment_id'],
                      'limit_usd': str(min(limits.limit_usd, conf.decimal(str(mapping(manifest['limits'])['limit_usd'])))),
                      'lifetime_minutes': limits.lifetime_minutes, 'box_extra_usd': str(limits.box_extra_usd),
                      'root_gib': root_gib,
                      **selection,
                      'prices': {key: str(value) for key, value in limits.spot_max_usd_per_hour.items()}}
            cloud_gate.save(path, online)
        if (folder / 'online-result.json').exists():
            return
        while True:
            try:
                self.run_boxes(folder, deployment, manifest, mode, online, allowed_types)
                return
            except cloud_gate.AwsServiceFailure as error:
                if not isinstance(mode, HanaGpuNextestRequest) or not error.first_attempt or error.code not in SPOT_QUOTA_REFUSALS:
                    raise
                refused = [*string_list(online.get('quota_refused_regions', [])), ledger.text(online, 'region')]
                prices = {size: conf.decimal(str(cap)) for size, cap in mapping(online['prices']).items() if size in allowed_types}
                candidate = self.select_nextest_offer(deployment, prices, excluded_regions=refused,
                    lifetime_minutes=int(str(online['lifetime_minutes'])), root_gib=int(str(online['root_gib'])),
                    extra_allowance=conf.decimal(str(online['box_extra_usd'])))
                if isinstance(candidate, NoEligibleHanaGpuOffer):
                    raise
                online.update(region=candidate.region, types=[candidate.instance_type], selected_zone=candidate.zone,
                              quota_refused_regions=refused)
                cloud_gate.save(path, {key: value for key, value in online.items() if key != 'bucket'})

    def run_boxes(self, folder: Path, deployment: dict[str, object], manifest: dict[str, object],
                  mode: HanaGpuNextestRequest | GpuSizeComparison, online: dict[str, object],
                  allowed_types: tuple[str, ...]) -> None:
        path = folder / 'online.json'
        empty: dict[str, object] = {}
        regional = next((row for row in ledger.rows(deployment, 'regions') if row['region'] == online['region']), empty)
        if not regional:
            raise ValueError('The saved GPU trial region has no gate deployment')
        regional_subnets = ledger.rows(regional, 'subnets')
        if isinstance(mode, GpuSizeComparison) and 'offerings' not in online:
            online['offerings'] = {size: [subnet['zone'] for subnet in self.offered_zones(regional, size)]
                                   for size in cast(list[str], online['types'])}
            cloud_gate.save(path, online)
        online['bucket'] = deployment['bucket']
        self.gates.charges()
        self.artifacts(folder, online)
        trial_id = ledger.text(online, 'trial')
        region = ledger.text(regional, 'region')
        for size in cast(list[str], online['types']):
            price = mapping(online['prices'])[size]
            attempt = gpu_attempt_id(trial_id, size, region if online.get('quota_refused_regions') else '')
            box = gpu_box(folder, trial_id, size, attempt)
            box.mkdir(parents=True, exist_ok=True)
            if (box / 'outcome.json').exists():
                continue
            result_path = box / 'result.json'
            previous = next((row for row in self.gates.book.read_snapshot().gate_reservations if row.attempt == attempt), None)
            if previous is not None:
                if isinstance(previous.state, (ledger.GateLaunchPending, ledger.GateAbsent)) and result_path.is_file():
                    result = read_json(result_path)
                    code = result.get('quota_refusal')
                    if isinstance(mode, HanaGpuNextestRequest) and isinstance(code, str) and code in SPOT_QUOTA_REFUSALS:
                        if isinstance(previous.state, ledger.GateLaunchPending):
                            self.gates.book.resolve_gate(attempt, ledger.GateAbsent(int(self.gates.clock())))
                        raise cloud_gate.AwsServiceFailure('run-instances', code, first_attempt=True)
                    if isinstance(mode, GpuSizeComparison) and result.get('status') == 'refused':
                        if isinstance(previous.state, ledger.GateLaunchPending):
                            self.gates.book.resolve_gate(attempt, ledger.GateAbsent(int(self.gates.clock())))
                        cloud_gate.save(box / 'outcome.json', result)
                        continue
                if not self.cleanup(folder):
                    raise ValueError('The previous GPU box termination remains unconfirmed')
                if isinstance(mode, HanaGpuNextestRequest):
                    self.finish_nextest(folder, box, size, attempt, manifest)
                    return
                cloud_gate.save(box / 'outcome.json', {'status': 'incomplete', 'reason': 'The service restarted'})
                continue
            maximum = conf.decimal(str(price))
            if isinstance(mode, HanaGpuNextestRequest):
                if size not in allowed_types:
                    raise ValueError('The gate deployment record does not allow the selected Hana GPU type. Update the gate setup.')
                subnet = next((row for row in regional_subnets if row['zone'] == online['selected_zone']), empty)
                if not subnet:
                    raise ValueError('The selected Hana GPU zone has no gate subnet')
            else:
                zones = string_list(mapping(online['offerings'])[size])
                subnets = [row for row in regional_subnets if row['zone'] in zones]
                offer = self.price_zone(regional, size, maximum, subnets) if subnets else 'not offered'
                if isinstance(offer, str):
                    print(size + ': ' + offer, flush=True)
                    cloud_gate.save(box / 'outcome.json', {'status': offer})
                    continue
                subnet = offer.subnet
            now = int(self.gates.clock())
            lifetime = int(str(online['lifetime_minutes'])) * 60
            expires = now + lifetime
            reservation_expires = expires
            gate_settings = cloud_gate.settings()
            if isinstance(mode, HanaGpuNextestRequest):
                stop_at = tag_expires = expires
                reservation_expires += HANA_EXPIRY_MARGIN_SECONDS
            else:
                expiry_backstop_s = gate_settings.whole('expiry_schedule_minutes') * 60 + gate_settings.whole('expiry_timeout_s')
                tag_expires = expires - expiry_backstop_s
                stop_at = min(expires - 300, tag_expires - 1)
            if stop_at <= now:
                raise ValueError('The GPU trial lifetime is too short for the expiry schedule')
            account = conf.read(self.gates.settings_path)
            root_gib = int(str(online['root_gib']))
            poll_seconds = gate_settings.whole('gpu_size_trial_result_poll_s')
            booked_seconds = reservation_expires - now
            extra_allowance = conf.decimal(str(online['box_extra_usd']))
            if isinstance(mode, GpuSizeComparison):
                extra_cost = gpu_box_extra_cost(gate_settings, account, region, root_gib=root_gib,
                                               booked_seconds=booked_seconds, poll_seconds=poll_seconds, keys_per_check=3)
                if extra_cost > extra_allowance:
                    raise ValueError(gpu_box_extra_refusal(extra_cost, extra_allowance))
            request: dict[str, object] = {'attempt': attempt, 'trial': online['trial'], 'mode': 'gpu-size-trial',
                'commit': manifest['commit'], 'instance_type': size, 'started': now, 'expires': expires, 'stop_at': stop_at}
            cloud_gate.save(box / 'request.json', request)
            reservation = ledger.GateReservation(attempt, now, reservation_expires, maximum,
                extra_allowance,
                instance_type=size, region=region, mode='gpu-size-trial', root_gib=root_gib,
                trial=ledger.GateTrialBudget(str(online['trial']), conf.decimal(str(online['limit_usd']))))
            self.gates.charges()
            decision = self.gates.book.reserve_gate(self.gates.settings_path, reservation)
            if not isinstance(decision, ledger.GateReserved):
                reason = decision.reason
                cloud_gate.save(box / 'outcome.json', {'status': 'budget stop', 'reason': reason})
                print('GPU size trial stopped: ' + reason, flush=True)
                cloud_gate.save(folder / 'online-result.json', {'trial': online['trial'], 'status': 'budget stop', 'reason': reason})
                return
            launch_failed = False
            try:
                links = self.signed_links(folder, online, request)
                policy = read_settings(self.gates.settings_path)
                tags = [{'Key': 'GateDeployment', 'Value': str(online['deployment_id'])},
                        {'Key': 'Workload', 'Value': 'cloud-claude-gate'}, {'Key': 'GateAttempt', 'Value': attempt},
                        {'Key': 'ExpiresAt', 'Value': str(tag_expires)}, {'Key': 'GpuSizeTrial', 'Value': str(online['trial'])},
                        {'Key': policy.cost_explorer_tag_key, 'Value': policy.cost_explorer_tag_value}]
                arguments: dict[str, object] = {
                    'LaunchTemplate': {'LaunchTemplateId': regional['template'], 'Version': regional['version']},
                    'ImageId': regional['image'], 'MinCount': 1, 'MaxCount': 1, 'ClientToken': attempt,
                    'InstanceType': size,
                    'InstanceMarketOptions': {'MarketType': 'spot', 'SpotOptions': {'SpotInstanceType': 'one-time',
                        'InstanceInterruptionBehavior': 'terminate', 'MaxPrice': str(maximum)}},
                    'BlockDeviceMappings': [{'DeviceName': account.text('root_device_name'),
                        'Ebs': {'VolumeSize': online['root_gib'], 'VolumeType': 'gp3', 'Encrypted': True, 'DeleteOnTermination': True}}],
                    'NetworkInterfaces': [{'DeviceIndex': 0, 'SubnetId': subnet['id'], 'Groups': [regional['security_group']],
                                           'AssociatePublicIpAddress': True}],
                    'TagSpecifications': [{'ResourceType': kind, 'Tags': tags} for kind in ('instance', 'volume')],
                    'UserData': gpu_user_data(links, str(manifest['system']), str(manifest['toolchain']), tag_expires, stop_at)}
                vcpus = largest_gate_vcpus((size,))
                if isinstance(mode, HanaGpuNextestRequest):
                    if self.available_gpu_vcpus(region, vcpus) < vcpus:
                        raise ValueError('The selected Hana GPU offer no longer fits the available Spot quota')
                    if isinstance(self.price_zone(regional, size, maximum, (subnet,)), str):
                        raise ValueError('The selected Hana GPU Spot price is unavailable or above the cap')
                    self.require_no_open_booking(current_attempt=attempt)
                    gate_settings = cloud_gate.settings()
                    poll_seconds = gate_settings.whole('gpu_size_trial_result_poll_s')
                    extra_cost = gpu_box_extra_cost(gate_settings, conf.read(self.gates.settings_path), region,
                        root_gib=root_gib, booked_seconds=booked_seconds, poll_seconds=poll_seconds, keys_per_check=1)
                    if extra_cost > extra_allowance:
                        raise ValueError(gpu_box_extra_refusal(extra_cost, extra_allowance))
                if self.gates.clock() >= stop_at:
                    raise ValueError('The GPU trial reached its stop time before launch')
                cloud_gate.save(box / 'launch.json', {'region': region, 'zone': subnet['zone']})
                self.gates.box_region = region
                self.gates.size_vcpus = vcpus
                try:
                    reply = self.gates.regional_api(region).call('run-instances', '--cli-input-json', json.dumps(arguments))
                except AwsUnavailable as error:
                    launch_failed = True
                    if (not isinstance(error, cloud_gate.AwsServiceFailure)
                            or not error.first_attempt or error.code not in RUN_INSTANCES_REFUSALS):
                        raise
                    if isinstance(mode, HanaGpuNextestRequest) and error.code in SPOT_QUOTA_REFUSALS:
                        cloud_gate.save(result_path, {'quota_refusal': error.code})
                    if isinstance(mode, GpuSizeComparison):
                        cloud_gate.save(result_path, {'status': 'refused', 'reason': str(error)})
                    self.gates.book.resolve_gate(attempt, ledger.GateAbsent(int(self.gates.clock())))
                    if isinstance(mode, HanaGpuNextestRequest):
                        raise
                else:
                    instance = ledger.text(ledger.rows(reply, 'Instances')[0], 'InstanceId')
                    self.gates.book.resolve_gate(attempt, ledger.GateRunning(instance), instance_type=size)
                    self.gates.record_launch(request, size, str(subnet['zone']), 'launched')
                    if isinstance(mode, HanaGpuNextestRequest):
                        self.collect_nextest(box, request, online, poll_seconds)
                    else:
                        self.collect(folder, box, request, online, poll_seconds)
            finally:
                if not self.cleanup(folder) and not launch_failed:
                    raise ValueError('GPU box termination remains unconfirmed; the next box cannot launch')
            if isinstance(mode, HanaGpuNextestRequest):
                self.finish_nextest(folder, box, size, attempt, manifest)
                return
            cloud_gate.save(box / 'outcome.json', read_json(result_path))
        cloud_gate.save(folder / 'online-result.json', {'trial': online['trial'], 'status': 'complete'})


    def finish_nextest(self, folder: Path, box: Path, size: str, attempt: str, manifest: dict[str, object]) -> None:
        result = box / 'finished.json'
        if not result.is_file():
            raise ValueError('The booked Hana GPU attempt has no result')
        _ = nextest_result(read_json(result), attempt, manifest)
        cloud_gate.save(folder / 'online-result.json', {'trial': manifest['trial'], 'status': 'complete',
                                                         'attempt': attempt, 'instance_type': size})


def online_main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description='Run the GPU trial service')
    modes = parser.add_mutually_exclusive_group(required=True)
    _ = modes.add_argument('--worker', type=Path)
    _ = modes.add_argument('--cleanup', type=Path)
    _ = parser.add_argument('--directory', type=Path, required=True)
    _ = parser.add_argument('--settings', type=Path, required=True)
    _ = parser.add_argument('--assigned', action='store_true')
    args = parser.parse_args(arguments)
    directory, settings_path = cast(Path, args.directory), cast(Path, args.settings)
    cleanup = cast(Path | None, args.cleanup)
    folder = cleanup if cleanup is not None else cast(Path, args.worker)
    try:
        folder = host_trial_folder(folder)
    except ValueError as error:
        print('GPU size trial refused: ' + str(error), file=sys.stderr)
        return 1
    configuration = conf.read(settings_path)
    api = AwsCli(configuration.text('credentials_profile'), configuration.text('region'),
                 cloud_gate.settings().number('api_timeout_s'), gpu_aws_executor(execute_command))
    worker = GpuTrialWorker(cloud_gate.GateWorker(api, ledger.CostLedger(directory), settings_path,
                                                folder / 'reservation.cgroup'))
    with control_lock(directory / 'gpu-size-trial'):
        try:
            if cleanup is not None:
                with held_trial(folder):
                    try:
                        if not worker.cleanup(folder):
                            return 1
                        if isinstance(nextest_request(read_json(folder / 'manifest.json')), HanaGpuNextestRequest):
                            cloud_gate.save(cleanup_receipt(folder), {'trial': folder.name})
                        return 0
                    finally:
                        if not (folder / 'online-result.json').exists() and not (folder / 'run-error.json').exists():
                            cloud_gate.save(folder / 'run-error.json', {'status': 'failed',
                                'reason': 'The online worker ended before it saved a final result'})
                        remove_run_inputs(folder, worker.gates.clock())
            deployment = gate_deployment(directory / 'gate-deployment.json', api.region)
            if cast(bool, args.assigned):
                confirm_assigned_gpu_service(folder.name)
            worker.execute(folder, deployment)
            return 0
        except (OSError, ValueError, KeyError, AwsUnavailable, subprocess.SubprocessError) as error:
            print('GPU size trial refused: ' + str(error), file=sys.stderr)
            return 1


@dataclass(frozen=True)
class AssignedHanaGpuLaunch:
    trial: str
    before_service: Callable[[], None]


@dataclass(frozen=True)
class StandaloneHanaGpuLaunch:
    pass


STANDALONE_HANA_GPU_LAUNCH = StandaloneHanaGpuLaunch()


def run_nextest(repo: Path, commit: str, request: HanaGpuNextestRequest, *,
                launch: AssignedHanaGpuLaunch | StandaloneHanaGpuLaunch = STANDALONE_HANA_GPU_LAUNCH
                ) -> HanaGpuNextestResult:
    folder = trial_root() / (launch.trial if isinstance(launch, AssignedHanaGpuLaunch) else uuid4().hex)
    with new_trial(folder, prune_previous=False):
        with redirect_stdout(sys.stderr):
            prepare_locked(repo, commit, folder, request)

        def service(command: Sequence[str]) -> int:
            if isinstance(launch, AssignedHanaGpuLaunch):
                launch.before_service()
            return subprocess.run(command, stdout=sys.stderr).returncode

        directory = ssh_target_state_directory('aws')
        settings = conf.path('aws_target')
        if isinstance(launch, AssignedHanaGpuLaunch):
            status = start_service(folder, directory, settings, service, assigned=True)
            if not assigned_gpu_cleanup_confirmed(launch.trial):
                if assigned_gpu_cleanup_pending(launch.trial):
                    raise ValueError('The Hana GPU service has no cleanup confirmation')
                remove_run_inputs(folder, datetime.now(UTC).timestamp())
                raise ValueError(f'The Hana GPU service did not start (status {status})')
        else:
            status = start_service(folder, directory, settings, service)
        receipt = folder / 'online-result.json'
        if receipt.is_file():
            outcome = read_json(receipt)
            if outcome.get('status') != 'complete':
                raise ValueError(str(outcome.get('reason', 'The Hana GPU worker did not complete')))
            size = ledger.text(outcome, 'instance_type')
            if size not in request.instance_types:
                raise ValueError('The Hana GPU result has the wrong instance type')
            attempt = ledger.text(outcome, 'attempt')
            manifest = read_json(folder / 'manifest.json')
            box = gpu_box(folder, ledger.text(manifest, 'trial'), size, attempt)
            return nextest_result(read_json(box / 'finished.json'), attempt, manifest)
        error = folder / 'run-error.json'
        if error.is_file():
            raise ValueError(ledger.text(read_json(error), 'reason'))
        remove_run_inputs(folder, datetime.now(UTC).timestamp())
        raise ValueError(f'The Hana GPU worker exited with status {status} and has no result')


def main(arguments: Sequence[str]) -> int:
    if arguments and arguments[0] != 'nextest' and ('--worker' in arguments or '--cleanup' in arguments):
        return online_main(arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('--prepare-worker', type=Path, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest='command')
    prepare_parser = commands.add_parser('prepare')
    _ = prepare_parser.add_argument('--commit', default='refs/remotes/origin/main')
    _ = prepare_parser.add_argument('--repo', type=Path, default=Path.home() / 'rust/hana')
    _ = prepare_parser.add_argument('--trial-folder', type=Path,
                                   default=Path.home() / '.local/state/gpu-size-trial' / uuid4().hex)
    compare_parser = commands.add_parser('compare')
    _ = compare_parser.add_argument('--trial-folder', type=Path, required=True)
    nextest = commands.add_parser('nextest', help='Run one Hana nextest command on a temporary GPU Spot box')
    _ = nextest.add_argument('--repo', type=Path, required=True)
    _ = nextest.add_argument('--commit', required=True)
    _ = nextest.add_argument('--instance-types', required=True, help='A comma-separated list of permitted GPU types')
    _ = nextest.add_argument('nextest_arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(arguments)
    command = cast(str | None, args.command)
    try:
        worker = cast(Path | None, args.prepare_worker)
        if worker is not None:
            prepare_worker(host_trial_folder(worker))
        elif command == 'prepare':
            prepare(cast(Path, args.repo).expanduser().resolve(), cast(str, args.commit),
                    host_trial_folder(cast(Path, args.trial_folder)))
        elif command == 'compare':
            print(compare(host_trial_folder(cast(Path, args.trial_folder))), end='')
        elif command == 'nextest':
            arguments = cast(list[str], args.nextest_arguments)
            if arguments[:1] != ['--']:
                raise ValueError('Put -- before the nextest arguments')
            request = HanaGpuNextestRequest(tuple(arguments[1:]), tuple(cast(str, args.instance_types).split(',')))
            result = run_nextest(cast(Path, args.repo).expanduser().resolve(), cast(str, args.commit), request)
            print(json.dumps(asdict(result)))
            return result.exit_code
        else:
            parser.error('Choose prepare, compare, or nextest')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, GateRefused) as error:
        if command == 'nextest':
            print('Hana GPU infrastructure failure: ' + str(error), file=sys.stderr)
            return NEXTEST_INFRASTRUCTURE_EXIT
        print('GPU size trial refused: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))

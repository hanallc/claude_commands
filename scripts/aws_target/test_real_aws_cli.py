"""Validate fixture commands with the installed AWS CLI and a closed local port.

The scanner reads production Python files in this directory and its subdirectories.
It finds literal argument lists, simple aliases, and some local helpers.
It can miss arguments built at runtime, indirect calls, and shell commands.
During collection, the process guard checks AWS commands that reach subprocess.
"""
from __future__ import annotations

import ast
from collections.abc import Callable, Generator, Iterator, Sequence
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
import errno
from functools import lru_cache
from io import StringIO
from multiprocessing import get_context
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from types import CodeType
from typing import ParamSpec, TypeVar, cast, override
import unittest
from unittest import mock

from scripts.aws_target import admin_secrets, plan_identity, plan_run, test_deploy, test_plan_identity, test_test_identity
from scripts.aws_target.aws_cli import AwsCli, AwsExecutor, execute_command

SOURCE = Path(__file__).resolve().parent
MODEL = Path('iam/2010-05-08/service-2.sdk-extras.json')
T = TypeVar('T')
P = ParamSpec('P')

# Reuse fake calls. The source check identifies uncovered call sites.
SCENARIOS = {
    'test_aws_target.SpotPlacementTests': (
        'test_refusals_pause_growth_until_the_next_hour_and_keep_partial_scores',),
    'test_measurement.MeasurementTests': (
        'test_controller_schedules_one_account_charge_query_and_local_reports_never_repeat_it',),
    'test_deploy.DeploymentTests': (
        'test_regional_gate_stack_reuses_home_resources_and_copies_missing_image',
        'test_regional_stack_whose_first_create_failed_is_removed_before_the_new_deploy',
        'test_gate_setup_proves_each_saved_gpu_type_and_the_result_transfer',
        'test_quota_request_uses_admin_credentials_and_continues_after_regional_failures',
        'test_down_removes_regional_stack_and_copied_image_before_snapshot',
        'test_down_removes_resources_and_preserves_spend_and_other_profiles',
        'test_create_saved_image_tags_the_image_and_snapshot',
        'test_a_limit_change_during_the_foundation_deploy_refuses_the_next_change',
        'test_create_reservation_failures_report_events_reason_status_and_redact',
        'test_foundation_preparation_refusal_discards_the_reservation',
        'test_empty_update_is_success_without_execution_or_compute_request',
        'test_lost_update_creation_response_discards_the_recovered_change_set',
        'test_quota_status_reads_with_plan_key_and_prints_newest_request_without_ids',
        'test_prices_lists_all_region_type_zone_combinations_without_ids',
        'test_scaler_key_cleans_stale_and_unconfirmed_keys',
        'test_remove_gate_image_switches_to_the_official_image_before_deleting_the_gate_image',
        'test_remove_gate_image_reads_events_when_the_update_wait_fails',
    ),
    'test_deploy.HanaSetupFailureTests': (
        'test_hana_setup_updates_only_iam_policies_and_saves_the_template_hash',
        'test_hana_down_resolves_a_pending_launch_before_teardown',
        'test_hana_down_waits_for_a_disk_hidden_after_aws_accepts_creation',
        'test_hana_down_recovers_the_request_and_disk_from_a_saved_instance',
    ),
    'test_hana_setup.HanaSetupTests': (
        'test_root_replacement_installs_the_scaler_policy_before_starting',
        'test_root_replacement_replays_the_token_and_polls_the_returned_task_id',
        'test_setup_reserves_costs_before_creating_resources_and_registers_only_after_stop',
        'test_existing_data_attachment_only_waits_for_the_volume',
        'test_image_upload_registers_imds_v2_root_disk_and_saves_the_zone_subnet',
    ),
    'test_hana_provider.HanaProviderTests': (
        'test_bootstrap_start_observe_publish_stop_and_policy_cover_the_same_calls',
        'test_execution_objects_use_the_exact_boot_and_scoped_policy',
        'test_bootstrap_recovery_reads_the_saved_user_data',
    ),
    'test_hana_transport.HanaTransportPolicyTests': (
        'test_source_upload_signing_and_policy_use_scoped_provider_operations',),
    'test_cloud_gate.CloudGateTests': (
        'test_expired_pending_sweep_searches_both_regions',
        'test_expired_uncertain_launch_keeps_reservation_until_reconciliation',
        'test_stopped_instance_reports_a_confirmed_spot_interruption',
        'test_reconcile_closes_the_missing_box_in_a_batch',
        'test_delta_upload_failure_uses_the_other_region_and_an_uncertain_retry_keeps_its_pin',
        'test_a_newer_regional_base_contains_the_entire_captured_source',
    ),
    'test_teardown.SourceBucketRemovalTests': (
        'test_down_retries_home_and_regional_stack_deletion_after_a_late_source_write',),
    'test_gpu_size_trial.GpuOnlineTests': (
        'test_upload_uses_s3_cp_at_the_multipart_limit',
        'test_hana_nextest_uses_the_cheapest_offer_within_quota_and_settles',
        'test_results_and_resolution_precede_next_launch',
    ),
    'test_spot_end.SpotEndTests': (
        'test_a_failed_create_leaves_no_request_box_disk_bucket_or_stack',
        'test_end_boxes_finds_an_untagged_request_from_the_box',
    ),
    'test_retained.RetainedTests': ('test_deleted_since_combines_every_matching_stack_by_id',),
    'test_plan_identity.PlanIdentityTests': (
        'test_create_encrypts_one_key_and_repeat_keeps_the_matching_key',
        'test_encryption_failure_revokes_the_key_and_keeps_output_private',
        'test_late_key_is_revoked_on_the_next_approved_run',
        'test_changed_config_updates_the_policy_without_replacing_the_key',
        'test_down_deletes_the_key_before_the_user_and_policy',
    ),
    'test_test_identity.TestIdentityTests': (
        'test_install_check_and_replace_key_for_same_account',),
    'test_plan_run.PlanSetupTests': (
        'test_stack_owned_plan_passes_with_the_current_allow_list',),
    'test_plan_run.GatePlanTests': (
        'test_existing_stacks_pass_in_configured_order',
        'test_plan_reuses_saved_regional_image_by_id',
    ),
}

type Operation = tuple[str, str]


def operation_index(arguments: Sequence[str]) -> int:
    index = 1
    while arguments[index] in ('--profile', '--region'):
        index += 2
    return index


@dataclass(frozen=True, order=True)
class CallSite:
    path: str
    line: int
    column: int

    @override
    def __str__(self) -> str:
        return f'{self.path}:{self.line}:{self.column + 1}'


@lru_cache
def source_path(filename: str) -> Path:
    return Path(filename).absolute().relative_to(SOURCE, walk_up=True)


@lru_cache
def source_positions(code: CodeType) -> tuple[tuple[int | None, int | None, int | None, int | None], ...]:
    return tuple(code.co_positions())


def source_frames() -> Iterator[CallSite]:
    frame = sys._getframe(1)  # pyright: ignore[reportPrivateUsage]
    while frame:
        relative = source_path(frame.f_code.co_filename)
        if relative.parts[0] != '..' and production_file(relative):
            line, _, column, _ = source_positions(frame.f_code)[frame.f_lasti // 2]
            if line is not None and column is not None:
                yield CallSite(str(relative), line, column)
        frame = frame.f_back


def production_file(path: Path) -> bool:
    return not path.name.startswith('test_') and not path.stem.endswith('_test_support')


@dataclass(frozen=True)
class ReplayFile:
    index: int
    root_offset: int
    relative_path: Path
    contents: bytes
    present: bool


@dataclass(frozen=True)
class CliCommand:
    arguments: tuple[str, ...]
    operation: Operation
    model: bytes
    files: tuple[ReplayFile, ...]


class CommandCollection:
    def __init__(self, root: Path) -> None:
        self.root: Path = root.resolve()
        self.commands: dict[CliCommand, set[CallSite]] = {}
        self.fixture: str = ''
        self.refusals: list[str] = []

    def refusal(self, reason: str) -> AssertionError:
        message = f'{self.fixture}: {reason}'
        self.refusals.append(message)
        return AssertionError(message)

    def fixture_path(self, name: str) -> Path:
        if not name.startswith(str(self.root) + '/') or not Path(os.path.normpath(name)).is_relative_to(self.root):
            raise self.refusal('AWS CLI argument refers to a file outside the fixture directory')
        path = Path(name).resolve()
        if not path.is_relative_to(self.root):
            raise self.refusal('AWS CLI argument refers to a file outside the fixture directory')
        if path != Path(os.path.normpath(name)):
            raise self.refusal('Cannot replay an AWS CLI file reference through a symbolic link')
        return path

    def file_arguments(self, arguments: Sequence[str], start: int) -> Iterator[tuple[int, int, bool]]:
        operation = tuple(arguments[start:start + 2])
        positional_files: set[int] = set()
        if operation == ('s3', 'cp'):
            operands = arguments[start + 2:start + 4]
            if len(operands) != 2 or any(word.startswith('-') for word in operands):
                raise self.refusal('Cannot classify the AWS s3 cp file arguments')
            positional_files.update(index for index, word in enumerate(operands, start + 2)
                                    if not word.startswith('s3://'))
        if operation == ('s3api', 'get-object'):
            options = {'--bucket', '--key', '--range', '--output'}
            index = start + 2
            while index < len(arguments):
                word = arguments[index]
                option, joined, _ = word.partition('=')
                if option in options:
                    index += 1 if joined else 2
                    continue
                if word.startswith('-'):
                    raise self.refusal('Cannot classify the AWS s3api get-object output file')
                positional_files.add(index)
                index += 1
            if len(positional_files) != 1:
                raise self.refusal('Cannot classify the AWS s3api get-object output file')

        for index, word in enumerate(arguments):
            previous = arguments[index - 1] if index else ''
            option, joined, value = word.partition('=')
            offset = len(option) + 1 if joined and option.startswith('--') else 0
            argument = value if offset else word
            file_option = option if offset else previous if not word.startswith('--') else ''
            if argument.startswith(('file://', 'fileb://')):
                yield index, offset + argument.index('://') + 3, True
            elif 'file://' in word or 'fileb://' in word:
                raise self.refusal('Cannot classify an AWS CLI file reference')
            elif len(file_option) > 2 and file_option.startswith('--') and any(
                    flag.startswith(file_option) for flag in ('--template-file', '--body', '--ca-bundle')):
                yield index, offset, not '--body'.startswith(file_option)
            elif index in positional_files:
                yield index, 0, False

    def collect(self, arguments: Sequence[str], environment: dict[str, str] | None = None) -> None:
        if Path(arguments[0]).name != 'aws':
            return
        start = operation_index(arguments)
        operation = arguments[start], arguments[start + 1]
        extras = (environment or {}).get('AWS_DATA_PATH')
        model = self.fixture_path(str(Path(extras) / MODEL)).read_bytes() if extras else b''
        words = list(arguments)
        files: list[ReplayFile] = []
        for index, offset, read_contents in self.file_arguments(arguments, start):
            word = arguments[index]
            name = self.fixture_path(word[offset:])
            relative = name.relative_to(self.root)
            present = name.is_file()
            contents = name.read_bytes() if read_contents and present else b''
            files.append(ReplayFile(index, offset, relative, contents, present))
            # Keep the exact suffix, including any trailing slash.
            words[index] = word[:offset] + word[offset + len(str(self.root)):]
        command = CliCommand(tuple(words), operation, model, tuple(files))
        self.commands.setdefault(command, set()).update(source_frames())

    def fake_binary(self, arguments: Sequence[str], environment: dict[str, str] | None) -> Path:
        path = shutil.which(arguments[0], path=(os.environ if environment is None else environment).get('PATH', ''))
        if path is None or not Path(path).resolve().is_relative_to(self.root):
            raise self.refusal('AWS command has no fixture executor')
        return Path(path).absolute()

    def execute_fixture_binary(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                               timeout: float, check: bool, env: dict[str, str] | None = None
                               ) -> subprocess.CompletedProcess[str]:
        path = self.fake_binary(arguments, env)
        return test_deploy.FixtureExecutor(path.parent.parent).run(
            [*arguments, '--cli-error-format', 'legacy'], capture_output=capture_output,
            text=text, timeout=timeout, check=check, env=env)

    def aws_executor(self, execute: AwsExecutor) -> AwsExecutor:
        def capture(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                    check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            self.collect(arguments, env)
            fake = self.execute_fixture_binary if execute is execute_command else execute
            return fake(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
        return capture

    def fixture_executor(self, execute: Callable[P, T]) -> Callable[P, T]:
        def capture(*args: P.args, **kwargs: P.kwargs) -> T:
            self.collect(cast(Sequence[str], args[1]), cast(dict[str, str] | None, kwargs.get('env')))
            return execute(*args, **kwargs)
        return capture


type ProcessWord = str | bytes | os.PathLike[str] | os.PathLike[bytes]


def shell_names_aws(command: str) -> bool:
    try:
        words = shlex.shlex(command, posix=True, punctuation_chars=True)
        words.whitespace_split = True
        if any(Path(word).name == 'aws' for word in words):
            return True
    except ValueError:
        # Check command text with incomplete shell quoting too.
        pass
    return re.search(r"""(?:^|[\s/|&;(<"'\`])aws(?=$|[\s|&;()<>"'\`])""", command) is not None


def run_fixtures(names: Sequence[str], collection: CommandCollection, endpoint: str, path: str) -> None:
    original_init = AwsCli.__init__
    original_plan_init = plan_run.PlanRun.__init__
    original_admin_executor = admin_secrets.AdminSecretSession.executor
    captured_executors: list[AwsExecutor] = []
    original_init_process = subprocess.Popen.__init__
    environment = fake_environment(collection.root, endpoint, path)

    def admin_executor(session: admin_secrets.AdminSecretSession, execute: admin_secrets.AdminExecutor, *,
                       single_attempt: bool = False) -> admin_secrets.AdminExecutor:
        wrapped = original_admin_executor(session, collection.aws_executor(execute), single_attempt=single_attempt)
        captured_executors.append(wrapped)
        return wrapped

    def init(api: AwsCli, profile: str, region: str, timeout_s: float,
             execute: AwsExecutor = execute_command) -> None:
        # The plan and admin wrappers capture after they remove the profile.
        if execute is execute_command or (profile != 'plan' and not any(
                execute is wrapped for wrapped in captured_executors)):
            execute = collection.aws_executor(execute)
            captured_executors.append(execute)
        original_init(api, profile, region, timeout_s, execute)

    def plan_init(plan: plan_run.PlanRun, load_credential: Callable[[], plan_identity.PlanCredentials],
                  executor: AwsExecutor, pause: Callable[[float], None]) -> None:
        original_plan_init(plan, load_credential, collection.aws_executor(executor), pause)

    def guarded_init(process: subprocess.Popen[str] | subprocess.Popen[bytes],
                     args: ProcessWord | Sequence[ProcessWord], *positional: object, **kwargs: object) -> None:
        words = [os.fsdecode(args)] if isinstance(args, (str, bytes, os.PathLike)) else list(map(os.fsdecode, args))
        extra = list(positional)
        executable = cast(ProcessWord | None, extra[1] if len(extra) > 1 else kwargs.get('executable'))
        program = Path(os.fsdecode(executable) if executable is not None else (words[0] if words else '')).name
        shell = extra[7] if len(extra) > 7 else kwargs.get('shell', False)
        commands = words[:1] if shell or isinstance(args, (str, bytes)) else []
        if program in ('sh', 'bash', 'zsh', 'dash', 'ksh'):
            commands.extend(words[index + 1] for index in range(1, len(words) - 1)
                            if re.fullmatch(r'-[a-zA-Z]*c[a-zA-Z]*|--command', words[index]))
        if any(shell_names_aws(command) for command in commands) and (shell or program != 'aws'):
            raise collection.refusal('AWS shell command has no fixture executor')
        if program == 'aws':
            if shell or not words or Path(words[0]).name != 'aws':
                raise collection.refusal('AWS command has no fixture executor')
            fixture_env = cast(dict[str, str] | None, extra[9] if len(extra) > 9 else kwargs.get('env'))
            fake = collection.fake_binary([os.fsdecode(executable)] if executable is not None else words, fixture_env)
            collection.collect(words, fixture_env)
            actual = dict(os.environ) if fixture_env is None else fixture_env
            safe_env = ({key: value for key, value in actual.items() if not key.startswith('AWS_')}
                        | {key: value for key, value in environment.items() if key.startswith('AWS_')})
            for index, key, value in ((1, 'executable', fake), (9, 'env', safe_env)):
                if len(extra) > index:
                    extra[index] = value
                else:
                    kwargs[key] = value
        cast(Callable[..., None], original_init_process)(process, args, *extra, **kwargs)

    output = StringIO()
    with (mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(tempfile, 'tempdir', str(collection.root)),
            mock.patch.object(subprocess.Popen, '__init__', new=guarded_init),
            mock.patch.object(AwsCli, '__init__', new=init),
            mock.patch.object(plan_run.PlanRun, '__init__', new=plan_init),
            mock.patch.object(admin_secrets.AdminSecretSession, 'executor', new=admin_executor),
            mock.patch.object(test_plan_identity.Commands, 'execute',
                              new=collection.fixture_executor(test_plan_identity.Commands.execute)),
            mock.patch.object(test_test_identity.IdentityCommands, 'execute',
                              new=collection.fixture_executor(test_test_identity.IdentityCommands.execute)),
            redirect_stdout(output), redirect_stderr(output)):
        for name in names:
            collection.fixture = name
            tests = unittest.TestLoader().loadTestsFromName(name)
            result = unittest.TextTestRunner(stream=output, verbosity=0).run(tests)
            if collection.refusals:
                raise AssertionError('\n'.join(collection.refusals))
            if not result.wasSuccessful() or result.skipped:
                skipped = ', '.join(test.id() for test, _ in result.skipped)
                raise AssertionError(f'{name}: fixture failed or skipped {skipped}\n' + output.getvalue()[-5500:])
            _ = output.seek(0)
            _ = output.truncate()


def literal(node: ast.expr) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ''


def call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        if node.attr == 'call' and ast.unparse(node.value) in ('subprocess', 'self.tailnet', 'probe'):
            return ''
        return node.attr
    if isinstance(node, ast.Call) and node.args and call_name(node.func) == 'partial':
        return call_name(node.args[0])
    return ''


def aws_calls(tree: ast.AST) -> Iterator[ast.Call]:
    builders = {'call', 'ec2_dry_run'}
    nodes = list(ast.walk(tree))
    assignments: list[tuple[str, ast.expr]] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            assignments.extend((target.id, node.value) for target in node.targets if isinstance(target, ast.Name))
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            assignments.append((node.target.id, node.value))
    imports = [alias for node in nodes if isinstance(node, ast.ImportFrom) for alias in node.names]
    functions = [node for node in nodes if isinstance(node, ast.FunctionDef)]
    commands: set[str] = set()
    def aws_arguments(node: ast.expr) -> bool:
        if isinstance(node, (ast.List, ast.Tuple)):
            return bool(node.elts and literal(node.elts[0]) == 'aws')
        return isinstance(node, ast.Name) and node.id in commands

    while True:
        aliases = {target for target, value in assignments if call_name(value) in builders}
        aliases.update(alias.asname for alias in imports if alias.asname and alias.name in builders)
        for function in functions:
            if function.name in builders:
                continue
            parameters = {argument.arg for argument in function.args.args}
            for call in ast.walk(function):
                if isinstance(call, ast.Call) and call_name(call.func) in builders:
                    # A helper that takes the operation as input builds the command at both sites.
                    selector_count = 2 if call.args and literal(call.args[0]) == 'iam' else 1
                    selectors = {part.id for arg in call.args[:selector_count] for part in ast.walk(arg)
                                 if isinstance(part, ast.Name)}
                    if selectors & parameters:
                        aliases.add(function.name)
        argv_aliases = {target for target, value in assignments if aws_arguments(value)}
        if aliases <= builders and argv_aliases <= commands:
            break
        builders.update(aliases)
        commands.update(argv_aliases)

    for node in nodes:
        if isinstance(node, ast.Call) and (call_name(node.func) in builders
                                           or any(aws_arguments(arg) for arg in node.args)):
            yield node


def source_calls(root: Path = SOURCE) -> set[CallSite]:
    return {CallSite(str(path.relative_to(root)), node.lineno, node.col_offset)
            for path in root.rglob('*.py') if production_file(path)
            for node in aws_calls(ast.parse(path.read_text(), filename=str(path)))}


def fake_environment(root: Path, endpoint: str, path: str) -> dict[str, str]:
    config = root / 'aws-config'
    credentials = root / 'aws-credentials'
    config.touch()
    credentials.touch()
    return {
        'PATH': path, 'HOME': str(root), 'TMPDIR': str(root), 'AWS_CONFIG_FILE': str(config),
        'AWS_SHARED_CREDENTIALS_FILE': str(credentials), 'AWS_MAX_ATTEMPTS': '1', 'AWS_ENDPOINT_URL': endpoint,
        'AWS_EC2_METADATA_DISABLED': 'true', 'AWS_PAGER': '',
        'AWS_ACCESS_KEY_ID': 'AKIAFAKEFAKEFAKE', 'AWS_SECRET_ACCESS_KEY': 'fixture-secret',
    }


def validate(binary: str, root: Path, endpoint: str, item: tuple[int, CliCommand]) -> str:
    index, command = item
    service, operation = command.operation
    words = command.arguments
    profile = words[words.index('--profile') + 1] if '--profile' in words else ''
    work = root / f'call-{index}'
    work.mkdir()
    environment = fake_environment(work, endpoint, str(Path(binary).parent) + os.pathsep + os.defpath)
    environment.update(HTTPS_PROXY=endpoint, HTTP_PROXY=endpoint, ALL_PROXY=endpoint, NO_PROXY='')
    arguments = list(words)
    for file in command.files:
        if file.present:
            local = work / file.relative_path
            local.parent.mkdir(parents=True, exist_ok=True)
            _ = local.write_bytes(file.contents)
        word = words[file.index]
        arguments[file.index] = word[:file.root_offset] + str(work) + word[file.root_offset:]
    if profile:
        credentials = work / 'credentials'
        _ = credentials.write_text(f'[{profile}]\naws_access_key_id = AKIAFAKEFAKEFAKE\naws_secret_access_key = fixture-secret\n')
        environment['AWS_SHARED_CREDENTIALS_FILE'] = str(credentials)
        del environment['AWS_ACCESS_KEY_ID'], environment['AWS_SECRET_ACCESS_KEY']
    if command.model:
        model = work / MODEL
        model.parent.mkdir(parents=True)
        _ = model.write_bytes(command.model)
        environment['AWS_DATA_PATH'] = str(work)
    arguments = [binary, '--endpoint-url', endpoint, *arguments[1:]]
    try:
        if '--cli-error-format' in words:
            result = subprocess.run(arguments, capture_output=True, text=True, env=environment, timeout=15, check=False)
        else:
            result = execute_command(['aws', *arguments[1:]], capture_output=True, text=True, env=environment, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        return f'{service} {operation}: the CLI timed out'
    if (service, operation) == ('configure', 'export-credentials') and result.returncode == 0:
        return ''
    error = result.stderr.strip().splitlines()
    if result.returncode != 252 and error:
        endpoint_error = rf'Could not connect to the endpoint URL: "{re.escape(endpoint)}(?:[/?][^"]*)?"'
        proxy_error = rf'Failed to connect to proxy URL: "{re.escape(endpoint)}"'
        transfer = r'(?:(?:upload|download|copy) failed: .+ to .+ )?' if command.operation == ('s3', 'cp') else ''
        last_line = error[-1].removeprefix('aws: [ERROR]: ')
        if re.fullmatch(rf'{transfer}(?:{endpoint_error}|{proxy_error})', last_line):
            return ''
    return f'{service} {operation}: exit {result.returncode}: {error[-1][:460] if error else "no error text"}'


def scenario_names() -> list[str]:
    return [f'scripts.aws_target.{fixture}.{name}' for fixture, scenario in SCENARIOS.items() for name in scenario]


def fixture_commands(root: Path, endpoint: str, path: str, name: str) -> dict[CliCommand, set[CallSite]]:
    with tempfile.TemporaryDirectory(dir=root) as folder:
        collection = CommandCollection(Path(folder))
        run_fixtures([name], collection, endpoint, path)
        return collection.commands


@contextmanager
def closed_endpoint() -> Generator[tuple[str, int]]:
    with socket.socket() as closed:
        closed.bind(('127.0.0.1', 0))
        yield cast(tuple[str, int], closed.getsockname())


class RealAwsCliTests(unittest.TestCase):
    def test_scanner_finds_nested_alias_and_both_helper_call_sites(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            example = root / 'nested' / 'example.py'
            example.parent.mkdir()
            _ = example.write_text(
                'def helper(api, operation):\n'
                + '    alias = api.call\n'
                + '    return alias(operation)\n'
                + 'helper(api, "start-instances")\n'
                + 'helper(api, "start-instances")\n')
            self.assertEqual(source_calls(root), {CallSite('nested/example.py', line, column)
                             for line, column in ((3, 11), (4, 0), (5, 0))})

    def test_deduplication_preserves_parser_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            collection = CommandCollection(root)
            for timestamp in ('2026-01-01T00:00:00Z', 'bad-timestamp'):
                collection.collect(('aws', 'ec2', 'describe-spot-price-history', '--start-time', timestamp))
            path = root / 'input.json'
            for key in ('FIXTUREKEY0000000000', 'FIXTUREKEY0000000000', 'short'):
                _ = path.write_text('{"AccessKeyId":"' + key + '"}')
                collection.collect(('aws', 'iam', 'delete-access-key', '--cli-input-json', 'file://' + str(path)))
            self.assertEqual(len(collection.commands), 4)

    def test_replay_preserves_file_path_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixture = root / 'fixture'
            fixture.mkdir()
            path = fixture / 'input.json'
            contents = b'{"AccessKeyId":"FIXTUREKEY0000000000"}'
            _ = path.write_bytes(contents)
            collection = CommandCollection(fixture)
            endpoint = 'http://127.0.0.1:0'
            spellings = [(scheme, slash) for scheme in ('file://', 'fileb://') for slash in ('', '/')]
            for scheme, slash in spellings:
                collection.collect(('aws', 'iam', 'delete-access-key', '--cli-input-json=' + scheme + str(path) + slash))
            self.assertEqual(len(collection.commands), 4)
            for index, (command, (scheme, slash)) in enumerate(zip(collection.commands, spellings, strict=True)):
                replay_path = root / f'call-{index}' / path.name
                with mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(
                        [], 255, '', f'Could not connect to the endpoint URL: "{endpoint}"')) as run:
                    self.assertEqual(validate('/bin/aws', root, endpoint, (index, command)), '')
                    run.assert_called_once()
                    replayed = cast(Sequence[str], run.call_args.args[0])
                    self.assertIn('--cli-input-json=' + scheme + str(replay_path) + slash, replayed)
                    self.assertEqual(replay_path.read_bytes(), contents)

    def test_collection_refuses_the_default_executor_and_files_outside_the_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            collection = CommandCollection(Path(folder))
            collection.fixture = 'fixture-without-fake'
            def missing_fake() -> None:
                self.assertEqual(os.environ['AWS_ACCESS_KEY_ID'], 'AKIAFAKEFAKEFAKE')
                self.assertEqual(os.environ['AWS_ENDPOINT_URL'], 'http://127.0.0.1:0')
                self.assertEqual(os.environ['AWS_EC2_METADATA_DISABLED'], 'true')
                self.assertEqual(Path(os.environ['AWS_CONFIG_FILE']).parent, collection.root)
                self.assertEqual(Path(os.environ['AWS_SHARED_CREDENTIALS_FILE']).parent, collection.root)
                self.assertNotIn('AWS_PROFILE', os.environ)
                with self.assertRaisesRegex(AssertionError, 'AWS command has no fixture executor'):
                    _ = AwsCli('fixture', 'us-east-1', 1).call('get-caller-identity', service='sts')

            with (mock.patch.dict(os.environ, {'AWS_PROFILE': 'ambient'}),
                  mock.patch.object(unittest.TestLoader, 'loadTestsFromName',
                                    return_value=unittest.TestSuite([unittest.FunctionTestCase(missing_fake)]))):
                with self.assertRaisesRegex(AssertionError, 'fixture-without-fake: AWS command has no fixture executor'):
                    run_fixtures(['fixture-without-fake'], collection, 'http://127.0.0.1:0', folder)
            with mock.patch.object(Path, 'read_bytes') as read:
                with self.assertRaisesRegex(AssertionError, 'outside the fixture directory'):
                    collection.collect(('aws', 'iam', 'delete-access-key', '--cli-input-json', 'file:///outside.json'))
                read.assert_not_called()

    def test_process_guard_refuses_aws_outside_the_fake(self) -> None:
        def check_output() -> None:
            _ = subprocess.check_output(['/outside/bin/aws', 'sts', 'get-caller-identity'])

        def popen() -> None:
            with subprocess.Popen(args=['aws', 'sts', 'get-caller-identity']):
                self.fail('Started AWS')

        def override_executable() -> None:
            with subprocess.Popen(['other', 'sts', 'get-caller-identity'], executable='/outside/bin/aws'):
                self.fail('Started AWS with an executable override')

        def shell_string() -> None:
            _ = subprocess.call('true && "/outside/bin/aws" sts get-caller-identity', shell=True)

        def shell_arguments() -> None:
            _ = subprocess.call(['sh', '-c', 'a\\ws sts get-caller-identity'])

        for fixture in (check_output, popen, override_executable, shell_string, shell_arguments):
            name = fixture.__name__
            with (self.subTest(name=name), tempfile.TemporaryDirectory() as folder,
                  mock.patch.object(subprocess.Popen, '_execute_child',
                                    side_effect=AssertionError('A process passed the guard')) as start,
                  mock.patch.object(unittest.TestLoader, 'loadTestsFromName',
                                    return_value=unittest.TestSuite([unittest.FunctionTestCase(fixture)]))):
                with self.assertRaisesRegex(AssertionError, f'{name}: AWS (?:shell )?command has no fixture executor'):
                    run_fixtures([name], CommandCollection(Path(folder)), 'http://127.0.0.1:0', folder)
                start.assert_not_called()

    def test_collection_checks_joined_binary_and_relative_file_references(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(Path, 'read_bytes') as read:
            collection = CommandCollection(Path(folder))
            collection.fixture = 'file-fixture'
            cases = (
                ('iam', 'delete-access-key', '--cli-input-json=file:///outside.json'),
                ('iam', 'delete-access-key', '--cli-input-json=fileb:///outside.json'),
                ('iam', 'delete-access-key', '--cli-input-json', 'file://relative.json'),
                ('s3api', 'put-object', '--body=relative.txt'),
                ('s3api', 'get-object', '--bucket', 'fixture', '--key', 'result', '--range', 'bytes=0-100',
                 'relative.txt', '--output', 'json'),
                ('s3', 'cp', 'relative.txt', 's3://fixture/result'),
            )
            for words in cases:
                with self.subTest(words=words):
                    with self.assertRaisesRegex(AssertionError, 'file-fixture: .*outside the fixture directory'):
                        collection.collect(('aws', *words))
            with self.assertRaisesRegex(AssertionError, 'file-fixture: Cannot classify .*file reference'):
                collection.collect(('aws', 'ec2', 'run-instances', '--block-device-mappings',
                                    'DeviceName@=file:///outside.txt'))
            read.assert_not_called()

    def test_skipped_fixture_fails_collection(self) -> None:
        class SkippedFixture(unittest.TestCase):
            def runTest(self) -> None:
                self.skipTest('fixture skip')
        with (tempfile.TemporaryDirectory() as folder,
              mock.patch.object(unittest.TestLoader, 'loadTestsFromName',
                                return_value=unittest.TestSuite([SkippedFixture()]))):
            with self.assertRaisesRegex(AssertionError, 'skip-fixture: fixture failed or skipped'):
                run_fixtures(['skip-fixture'], CommandCollection(Path(folder)),
                             'http://127.0.0.1:0', os.environ['PATH'])

    def test_bound_endpoint_refuses_connections(self) -> None:
        with socket.socket() as second:
            with closed_endpoint() as address, socket.socket() as client:
                with self.assertRaises(OSError) as error:
                    second.bind(address)
                self.assertEqual(error.exception.errno, errno.EADDRINUSE)
                self.assertEqual(client.connect_ex(address), errno.ECONNREFUSED)
            second.bind(address)

    def test_endpoint_override_cannot_bypass_the_closed_proxy(self) -> None:
        binary = shutil.which('aws')
        if binary is None:
            self.skipTest('The aws binary is absent')
        with tempfile.TemporaryDirectory() as folder, closed_endpoint() as address:
            root = Path(folder)
            endpoint = f'http://{address[0]}:{address[1]}'
            collection = CommandCollection(root)
            for option in ('--endpoint-url', '--endpoint-ur'):
                collection.collect(('aws', '--region', 'us-east-1', 'sts', 'get-caller-identity',
                                    option, 'https://example.invalid'))
            source = root / 'source.txt'
            source.touch()
            collection.collect(('aws', '--region', 'us-east-1', 's3', 'cp', str(source), 's3://fixture/source.txt',
                                '--endpoint-url', 'https://example.invalid', '--only-show-errors'))
            for index, command in enumerate(collection.commands):
                with self.subTest(operation=command.operation, arguments=command.arguments):
                    self.assertEqual(validate(binary, root, endpoint, (index, command)), '')

    def test_fixture_commands_pass_the_real_aws_cli_parser(self) -> None:
        binary = shutil.which('aws')
        if binary is None:
            self.skipTest('The aws binary is absent')
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as folder, closed_endpoint() as address:
            endpoint = f'http://{address[0]}:{address[1]}'
            root = Path(folder)
            expected = source_calls()
            origins: dict[CliCommand, set[CallSite]] = {}
            replayed: dict[CliCommand, Future[str]] = {}
            with (ThreadPoolExecutor(max_workers=32) as replays,
                  ProcessPoolExecutor(max_workers=8, mp_context=get_context('spawn')) as fixtures):
                jobs = [fixtures.submit(fixture_commands, root, endpoint, os.environ['PATH'], name)
                        for name in scenario_names()]
                for job in as_completed(jobs):
                    for command, sites in job.result().items():
                        origins.setdefault(command, set()).update(sites)
                        if command not in replayed:
                            replayed[command] = replays.submit(validate, binary, root, endpoint,
                                                               (len(replayed), command))
                covered = {site for sites in origins.values() for site in sites}
                missing = sorted(expected - covered)
                if missing:
                    self.fail('Uncovered AWS call sites:\n' + '\n'.join(map(str, missing)))
                collected = time.monotonic()
                errors: list[str] = []
                for command, verdict in replayed.items():
                    error = verdict.result()
                    if error:
                        sites = ', '.join(map(str, sorted(origins[command] & expected)))
                        errors.append(f'{error} [{sites}]')
        operations = {command.operation for command in replayed}
        print(f'Real AWS CLI: {len(replayed)} argument lists, {len(expected)} call sites, '
              + f'{len(operations)} operation pairs, {time.monotonic() - started:.1f} s '
              + f'({collected - started:.1f} s collection)', flush=True)
        self.assertEqual(list(dict.fromkeys(errors)), [])

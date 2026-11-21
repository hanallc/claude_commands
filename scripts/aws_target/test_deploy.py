"""Exercise guarded deployment with a private AWS executable and no network."""
from __future__ import annotations
from scripts.build_targets.target_test_support import mac_target_settings
from scripts.lint.memory_test_support import write_config_fixture

from collections.abc import Callable, Generator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import threading
import time
from types import CodeType, SimpleNamespace
from typing import ClassVar, Literal, cast, override
from unittest import mock


from scripts.aws_target import admin_secrets, aws_cli, aws_target, aws_trial, box, config as aws_config, deploy, gate_box, gate_probe, gpu_size_trial, hana_box, hana_image, hana_render, hana_spend, ledger, plan_identity, plan_run, remote
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.controller_lock import HanaCommandAccess, account_stack_lock, hana_command_lock
from scripts.aws_target.ledger_test_support import CostFixture, account_reply, numeric_settings, text_setting, write_hana_config
from scripts.aws_target.hana_provider import AwsHanaProvider
from scripts.aws_target.measurement_test_support import DAY, narrow
from scripts.aws_target.test_box import SYSTEM, FakeTailnet, write_aws_claim
from scripts.aws_target.test_spot_end import Disk, FakeEc2, TAG as HANA_TAG
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import conf, file_lock
from scripts.mac_test import mac_test
from scripts.build_targets import target_config
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


QUOTA_PLAN_KEY = plan_identity.PlanCredentials('AKIAFIXTUREEXAMPLE00', 'Fixture000' * 4,
                                               plan_identity.PlanAccount('aws', '123456789012'))


FAKE_AWS = r'''import datetime, json, os, pathlib, sys
root = pathlib.Path(os.environ['DEPLOY_FIXTURE'])
args = sys.argv[1:]
service_index = args.index('--region') + 2
service = args[service_index]
operation = args[service_index + 1]
if service == 'ssm' and os.environ.get('BOX_FIXTURE'):
    import runpy
    sys.argv = [str(root / 'box-aws'), *args]
    runpy.run_path(sys.argv[0], run_name='__main__')
    sys.exit(0)
if (root / ('fail-down-' + operation)).exists():
    print('fixture removal failure: ' + operation, file=sys.stderr)
    sys.exit(1)
state_path = root / 'fake-stack.json'
state = json.loads(state_path.read_text()) if state_path.exists() else {}
clock = int((root / 'clock').read_text())
book = root / 'state/cost-ledger.json'
evidence = json.loads(book.read_text()) if book.exists() else {}
with (root / 'commands.jsonl').open('a') as output:
    output.write(json.dumps({'args': args if '--profile' in args else ['--profile', 'environment', *args], 'decision': evidence.get('decision', {}),
                             'compute_requests': evidence.get('compute_requests', [])}) + '\n')
def reply(value):
    print(json.dumps(value))
override = root / (operation + '.json')
if override.exists():
    reply(json.loads(override.read_text()))
    sys.exit(0)
keys_path = root / 'fake-keys.json'
keys = json.loads(keys_path.read_text()) if keys_path.exists() else ['FIXTUREOLDKEY0000000']
if service == 'sts' and operation == 'get-caller-identity':
    if '--profile' not in args:
        reply({'Account': '123456789012'})
        sys.exit(0)
    import configparser
    credentials = configparser.ConfigParser()
    credentials.read(os.environ['AWS_SHARED_CREDENTIALS_FILE'])
    profile = args[args.index('--profile') + 1]
    valid = credentials.get(profile, 'aws_access_key_id', fallback='') == 'FIXTURENEWKEY0000000'
    if not valid or (root / 'fail-scaler').exists():
        print('The profile is not ready', file=sys.stderr)
        sys.exit(1)
    reply({'Arn': 'arn:aws:iam::123456789012:user/build-scaler'})
elif service == 'iam' and operation == 'get-user':
    if (root / 'deleted-stack').exists():
        print('NoSuchEntity', file=sys.stderr); sys.exit(1)
    user = args[args.index('--user-name') + 1]
    reply({'User': {'Arn': 'arn:aws:iam::123456789012:user/' + user, 'UserId': 'user-fixture'}})
elif service == 'cloudformation' and operation == 'describe-stack-resource':
    logical = args[args.index('--logical-resource-id') + 1]
    reply({'StackResourceDetail': {'PhysicalResourceId': state['outputs']['InstanceId'] if logical == 'Instance' else 'build-scaler'}})
elif service == 'cloudformation' and operation == 'wait':
    if (root / ('fail-wait-' + args[service_index + 2])).exists():
        print('fixture wait failed', file=sys.stderr); sys.exit(1)
    reply({})
elif service == 'cloudformation' and operation == 'get-template':
    reply({'TemplateBody': json.loads((root / 'live-template.json').read_text())})
elif service == 'cloudformation' and operation == 'describe-stack-events':
    reply({'StackEvents': []})
elif service == 'cloudformation' and operation == 'delete-change-set':
    name = args[args.index('--change-set-name') + 1]
    (root / ('update-change-set.json' if name.startswith('update-') else 'change-set.json')).unlink(missing_ok=True); reply({})
elif service == 'cloudformation' and operation == 'describe-change-set':
    change = root / ('update-change-set.json' if args[args.index('--change-set-name') + 1].startswith('update-') else 'change-set.json')
    if not change.exists():
        print('ChangeSetNotFound: does not exist', file=sys.stderr); sys.exit(1)
    reply(json.loads(change.read_text()))
elif service == 'cloudformation' and operation == 'create-change-set':
    name = args[args.index('--stack-name') + 1]
    stack_id = 'arn:aws:cloudformation:us-east-1:123456789012:stack/' + name + '/fixture-id'
    updating = args[args.index('--change-set-type') + 1] == 'UPDATE'
    if updating:
        stack_id = state['stack_id']
    (root / 'reserved-stack').write_text(stack_id)
    change = root / ('update-change-set.json' if updating else 'change-set.json')
    assert not change.exists() or updating, 'duplicate change-set name'
    if updating:
        names = root / 'update-names.json'
        used = json.loads(names.read_text()) if names.exists() else []
        change_name = args[args.index('--change-set-name') + 1]
        assert change_name not in used, 'duplicate update name'
        names.write_text(json.dumps([*used, change_name]))
    change.write_text(json.dumps({'StackId': stack_id, 'Status': 'CREATE_COMPLETE', 'Changes': [],
                                 'Parameters': json.loads(args[args.index('--parameters') + 1]),
                                 'Template': json.loads(pathlib.Path(args[args.index('--template-body') + 1].removeprefix('file://')).read_text())}))
    if updating and (root / 'update-plan.json').exists():
        value = json.loads(change.read_text())
        value.update(json.loads((root / 'update-plan.json').read_text()))
        change.write_text(json.dumps(value))
    if (root / 'lose-create-reply').exists():
        print('lost CREATE response', file=sys.stderr); sys.exit(1)
    reply({'StackId': stack_id})
elif service == 'iam' and operation == 'create-access-key':
    keys.append('FIXTURENEWKEY0000000')
    keys_path.write_text(json.dumps(keys))
    reply({'AccessKey': {'AccessKeyId': 'FIXTURENEWKEY0000000', 'SecretAccessKey': 'fixture-secret-only-in-credentials'}})
elif service == 'iam' and operation == 'list-access-keys':
    if (root / 'deleted-stack').exists():
        print('NoSuchEntity', file=sys.stderr); sys.exit(1)
    reply({'AccessKeyMetadata': [{'AccessKeyId': key} for key in keys]})
elif service == 'iam' and operation == 'delete-access-key':
    keys.remove(args[args.index('--access-key-id') + 1])
    keys_path.write_text(json.dumps(keys))
    reply({})
elif service == 'cloudformation' and operation == 'describe-stacks':
    if not state and ((root / 'review-stack').exists() or (root / 'change-set.json').exists()):
        reply({'Stacks': [{'StackStatus': 'REVIEW_IN_PROGRESS', 'StackId': (root / 'reserved-stack').read_text() if (root / 'reserved-stack').exists() else ''}]})
        sys.exit(0)
    if not state or args[args.index('--stack-name') + 1] not in (state.get('stack_name'), state.get('stack_id')):
        print('ValidationError: Stack does not exist', file=sys.stderr)
        sys.exit(1)
    reply({'Stacks': [{'StackId': state.get('stack_id'), 'StackStatus': state.get('status', 'UPDATE_COMPLETE'),
        'Parameters': [{'ParameterKey': k, 'ParameterValue': v} for k, v in state.get('parameters', {}).items()], 'Outputs': [
        {'OutputKey': key, 'OutputValue': value} for key, value in state['outputs'].items()]}]})
elif service == 'cloudformation' and operation == 'delete-stack':
    state_path.unlink(); (root / 'deleted-stack').touch(); reply({})
elif service == 'cloudformation' and operation in ('deploy', 'execute-change-set'):
    if operation == 'execute-change-set':
        change = json.loads((root / 'update-change-set.json').read_text())
        params = {row['ParameterKey']: state.get('parameters', {}).get(row['ParameterKey'], 'previous-bootstrap')
                  if row.get('UsePreviousValue') else row['ParameterValue'] for row in change['Parameters']}
    else:
        start = args.index('--parameter-overrides') + 1
        params = dict(word.split('=', 1) for word in args[start:] if '=' in word)
    owner = json.loads((root / 'state/deployment.json').read_text())
    outputs = {'BudgetName': params['BudgetName'] or owner['stack'] + '-monthly-' + params['DeploymentId']}
    outputs.update({key: value for key, value in state.get('outputs', {}).items() if key.startswith('Gate')})
    if operation == 'execute-change-set':
        (root / 'live-template.json').write_text(json.dumps(change['Template']))
    owner = json.loads((root / 'state/deployment.json').read_text())
    assert owner['account_id'] == ('210987654321' if owner['stack'] == 'aws-build' else '123456789012')
    assert owner['stack_id'] == (root / 'reserved-stack').read_text()
    assert owner['deployment_id'] == params['DeploymentId']
    state = {'outputs': outputs, 'stack_name': owner['stack'], 'stack_id': owner['stack_id'], 'parameters': params,
             'instance_state': 'terminated'}
    (root / 'deleted-stack').unlink(missing_ok=True)
    (root / 'deleted-volume').unlink(missing_ok=True)
    (root / 'deleted-budget').unlink(missing_ok=True)
    state_path.write_text(json.dumps(state))
    (root / 'change-set.json').unlink(missing_ok=True)
    (root / 'update-change-set.json').unlink(missing_ok=True)
    if (root / 'slow-foundation').exists():
        (root / 'clock').write_text(str(clock + 301))
    reply({})
elif operation == 'create-image':
    reply({'ImageId': 'ami-cache'})
elif operation in ('deregister-image', 'delete-snapshot'):
    reply({})
elif operation == 'describe-snapshots':
    reply({'Snapshots': [{'SnapshotId': 'snap-cache'}]})
elif operation == 'describe-images' and '--owners' in args and args[args.index('--owners') + 1] == 'self':
    reply({'Images': [{'ImageId': 'ami-cache', 'State': 'available', 'BlockDeviceMappings': [{'DeviceName': '/dev/xvda', 'Ebs': {'SnapshotId': 'snap-cache'}}]}]})
elif operation == 'describe-images':
    reply({'Images': [{'ImageId': 'ami-fixture', 'OwnerId': '123456789012', 'Architecture': 'x86_64',
                       'State': 'available', 'Name': 'nixos-26.05-x86_64', 'RootDeviceName': '/dev/xvda'}]})
elif operation == 'describe-subnets':
    reply({'Subnets': [{'VpcId': 'vpc-fixture', 'AvailabilityZone': 'us-east-1a'}]})
elif operation == 'create-tags':
    (root / 'volume-owner').write_text(args[args.index('--tags') + 1].split('Value=', 1)[1]); reply({})
elif operation == 'describe-volumes':
    if (root / 'deleted-volume').exists():
        print('InvalidVolume.NotFound', file=sys.stderr); sys.exit(1)
    reply({'Volumes': [{'AvailabilityZone': 'us-east-1a', 'Encrypted': True, 'State': 'available',
                        'VolumeId': args[args.index('--volume-ids') + 1], 'Attachments': [],
                        'Size': 500, 'VolumeType': 'gp3', 'Iops': 6000, 'Throughput': 500,
                        'Tags': [{'Key': 'Project', 'Value': 'fixture-build'},
                                 {'Key': 'DeploymentId', 'Value': (root / 'volume-owner').read_text() if (root / 'volume-owner').exists() else ''},
                                 {'Key': 'aws:cloudformation:stack-id', 'Value': (root / 'reserved-stack').read_text() if (root / 'reserved-stack').exists() else ''}]}]})
elif operation == 'delete-volume':
    (root / 'deleted-volume').touch(); reply({})
elif service == 'budgets' and operation == 'describe-budget':
    if (root / 'deleted-budget').exists():
        print('NotFoundException', file=sys.stderr); sys.exit(1)
    reply({'Budget': {'BudgetName': args[args.index('--budget-name') + 1]}})
elif service == 'budgets' and operation == 'delete-budget':
    (root / 'deleted-budget').touch(); reply({})
elif operation == 'describe-instances':
    if '--filters' in args:
        reply({'Reservations': []}); sys.exit(0)
    if (root / 'missing-instance').exists():
        print('InvalidInstanceID.NotFound: instance does not exist', file=sys.stderr)
        sys.exit(1)
    if os.environ.get('BOX_FIXTURE'):
        live_path = root / 'live.json'
        live = json.loads(live_path.read_text())
        live['observations'] = live.get('observations', 0) + 1
        if live['observations'] >= live.get('stop_after', 3): live['ec2'] = 'stopped'
        live_path.write_text(json.dumps(live))
        state['instance_state'] = live['ec2']
    reply({'Reservations': [{'Instances': [{'InstanceId': 'i-fixture', 'LaunchTime': 'fixture-boot',
                                            'State': {'Name': state.get('instance_state', 'stopped')}}]}]})
elif operation == 'stop-instances':
    assert args[args.index('--instance-ids') + 1] == state['outputs']['InstanceId']
    state['instance_state'] = 'stopped'
    state_path.write_text(json.dumps(state))
    if os.environ.get('BOX_FIXTURE'):
        live_path = root / 'live.json'
        live = json.loads(live_path.read_text())
        live.update(ec2='stopped', forced_stop=True)
        live_path.write_text(json.dumps(live))
    reply({'StoppingInstances': [{'InstanceId': 'i-fixture', 'CurrentState': {'Name': 'stopped'}}]})
elif operation == 'start-instances':
    assert args[args.index('--instance-ids') + 1] == state['outputs']['InstanceId']
    capacity = root / 'capacity-refusals'
    if capacity.exists() and int(capacity.read_text()) > 0:
        capacity.write_text(str(int(capacity.read_text()) - 1))
        print('InsufficientInstanceCapacity: no Spot capacity', file=sys.stderr)
        sys.exit(1)
    state['instance_state'] = 'pending'
    state_path.write_text(json.dumps(state))
    if os.environ.get('BOX_FIXTURE'):
        live_path = root / 'live.json'
        live = json.loads(live_path.read_text())
        live.update(ec2='running', observations=0)
        live_path.write_text(json.dumps(live))
    reply({'StartingInstances': [{'InstanceId': 'i-fixture', 'CurrentState': {'Name': 'pending'}}]})
elif operation == 'describe-spot-price-history':
    reply({'SpotPriceHistory': [{'Timestamp': datetime.datetime.fromtimestamp(clock, datetime.UTC).isoformat(),
                                 'SpotPrice': '0.1'}]})
elif operation == 'get-service-quota':
    reply({'Quota': {'Value': 128}})
elif operation == 'get-cost-and-usage':
    period = args[args.index('--time-period') + 1]
    assert all(len(part.split('=')[1]) == 10 for part in period.split(','))
    bounds = dict(part.split('=', 1) for part in period.split(','))
    first, end = (datetime.datetime.fromisoformat(bounds[key]) for key in ('Start', 'End'))
    days = []
    while first < end:
        following = first + datetime.timedelta(days=1)
        days.append({'TimePeriod': {'Start': first.date().isoformat(), 'End': following.date().isoformat()},
                     'Estimated': False, 'Total': {}, 'Groups': [{'Keys': ['USE2-SpotUsage:m7a.4xlarge'],
                         'Metrics': {'UnblendedCost': {'Amount': '0.1', 'Unit': 'USD'},
                                     'UsageQuantity': {'Amount': '1', 'Unit': 'Hrs'}}}]})
        first = following
    reply({'ResultsByTime': days})
else:
    raise AssertionError((service, operation))
'''


@lru_cache(maxsize=None)
def compile_fake(source: str) -> CodeType:
    return compile(source, '<deployment fake>', 'exec')


def run_fake(path: str, *, run_name: str = '__main__') -> dict[str, object]:
    namespace: dict[str, object] = {'__name__': run_name, '__file__': path}
    exec(compile_fake(Path(path).read_text()), namespace)
    return namespace


class FixtureExecutor:
    """Run Python fixture commands without starting another interpreter."""

    def __init__(self, root: Path) -> None:
        self.root: Path = root

    def run(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
            timeout: float, check: bool, env: dict[str, str] | None = None,
            input: str | None = None) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ) if env is None else env
        found = shutil.which(arguments[0], path=environment.get('PATH'))
        path = Path(found) if found else Path(arguments[0])
        if (path.parent != self.root / 'bin' or not path.read_text().startswith(f'#!{sys.executable}\n')
                or input is not None):
            return subprocess.run(arguments, capture_output=capture_output, text=text, timeout=timeout,
                                  check=check, env=env, input=input)
        assert capture_output and text
        stdout, stderr = StringIO(), StringIO()
        status = 0
        with (mock.patch.dict(os.environ, environment, clear=True),
              mock.patch.object(sys, 'argv', [str(path), *arguments[1:]]),
              mock.patch.object(runpy, 'run_path', side_effect=run_fake),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            try:
                _ = run_fake(str(path))
            except SystemExit as exit:
                if exit.code is None:
                    status = 0
                elif isinstance(exit.code, int):
                    status = exit.code
                else:
                    print(exit.code, file=sys.stderr)
                    status = 1
        result = subprocess.CompletedProcess(arguments, status, stdout.getvalue(), stderr.getvalue())
        if check:
            result.check_returncode()
        return result


class GateImageAws:
    """Keep the gate stack, the saved gate image and its snapshot in local test state."""
    def __init__(self, failure: str = '') -> None:
        self.failure: str = failure
        self.commands: list[list[str]] = []
        self.version: int = 1
        self.gate_changes: list[dict[str, object]] = []
        self.image_parameter: str = 'ami-old'
        self.manifest: Path = Path()
        self.live_template: dict[str, object] = {'Resources': {
            'GateExpiryFunction': {'live': 'keep'}, 'GateScalerPolicy': {'Properties': {
                'PolicyDocument': {'Statement': [{'Effect': 'Allow', 'Action': ['ec2:RunInstances'],
                    'Resource': {'Fn::Sub': 'arn:${AWS::Partition}:ec2:${AWS::Region}::image/${GateImage}'}}]}}}}}
        self.images: dict[str, dict[str, object]] = {
            'ami-old': {'ImageId': 'ami-old', 'State': 'available', 'Tags': self.tags(),
                'BlockDeviceMappings': [{'DeviceName': '/dev/xvda', 'Ebs': {'SnapshotId': 'snap-old', 'VolumeSize': 32}}]},
            'ami-builder': {'ImageId': 'ami-builder', 'Tags': [{'Key': box.IMAGE_OWNER_TAG, 'Value': 'true'}]}}
        self.snapshots: dict[str, dict[str, object]] = {
            'snap-old': {'SnapshotId': 'snap-old', 'Tags': self.tags()},
            'snap-builder': {'SnapshotId': 'snap-builder', 'Tags': [{'Key': box.IMAGE_OWNER_TAG, 'Value': 'true'}]}}

    @staticmethod
    def tags() -> list[dict[str, str]]:
        return [{'Key': gate_box.GATE_IMAGE_OWNER_TAG, 'Value': 'true'}]

    def __call__(self, arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
        args = list(arguments)
        self.commands.append(args)
        operation = args[args.index('--region') + 3]
        reply: dict[str, object] = {}
        error = ''
        if operation == 'get-cost-and-usage':
            reply = account_reply(arguments)
        elif operation == 'describe-stacks':
            reply = {'Stacks': [{'StackStatus': 'UPDATE_COMPLETE', 'StackId': 'fixture-stack',
                'Parameters': [{'ParameterKey': 'GateImage', 'ParameterValue': self.image_parameter},
                               {'ParameterKey': 'VpcId', 'ParameterValue': 'vpc-fixture'}],
                'Outputs': [{'OutputKey': 'GateLaunchVersion', 'OutputValue': str(self.version)}]}]}
        elif operation == 'get-template':
            reply = {'TemplateBody': self.live_template}
        elif operation == 'create-change-set':
            self.gate_changes.append({'Parameters': json.loads(args[args.index('--parameters') + 1]),
                'Template': json.loads(Path(args[args.index('--template-body') + 1].removeprefix('file://')).read_text())})
        elif operation == 'describe-change-set':
            reply = {'Status': 'CREATE_COMPLETE', 'Changes': [
                {'ResourceChange': {'LogicalResourceId': 'GateLaunchTemplate'}}]}
        elif operation == 'execute-change-set':
            if self.failure == 'activate':
                error = 'fixture activation failure'
            else:
                change = self.gate_changes[-1]
                self.live_template = ledger.mapping(change['Template'])
                for row in ledger.rows(change, 'Parameters'):
                    if row['ParameterKey'] == 'GateImage' and 'ParameterValue' in row:
                        image = ledger.text(row, 'ParameterValue')
                        if image != self.image_parameter:
                            self.version += 1
                            self.image_parameter = image
        elif operation == 'wait':
            if 'stack-update-complete' in args and (self.failure == 'waiter'
                    or self.failure == 'narrow waiter' and len(self.gate_changes) == 2):
                error = 'fixture stack waiter failure'
        elif operation == 'describe-stack-events':
            reply = {'StackEvents': []}
        elif operation == 'delete-change-set':
            pass
        elif operation in ('describe-images', 'describe-snapshots'):
            owner = ledger.text(ledger.rows({'rows': cast(object, json.loads(args[args.index('--filters') + 1]))},
                                            'rows')[0], 'Name').removeprefix('tag:')
            tags = [{'Key': owner, 'Value': 'true'}]
            if operation == 'describe-images':
                reply = {'Images': [row for row in self.images.values() if row.get('Tags') == tags]}
            else:
                reply = {'Snapshots': [row for row in self.snapshots.values() if row['Tags'] == tags]}
        elif operation == 'deregister-image':
            del self.images[args[args.index('--image-id') + 1]]
        elif operation == 'delete-snapshot':
            del self.snapshots[args[args.index('--snapshot-id') + 1]]
        else:
            raise AssertionError(operation)
        if self.manifest.exists():
            record = remote.read_record(self.manifest)
            selected = ledger.text(ledger.rows(record, 'regions')[0] if 'regions' in record else record, 'image')
            if selected not in self.launchable_images():
                raise AssertionError(f'The selected image {selected} has no launch permission')
        return subprocess.CompletedProcess(args, int(bool(error)), json.dumps(reply), error)

    def launchable_images(self) -> set[str]:
        policy = ledger.mapping(ledger.mapping(ledger.mapping(
            ledger.mapping(self.live_template['Resources'])['GateScalerPolicy'])['Properties'])['PolicyDocument'])
        transition = [row for row in ledger.rows(policy, 'Statement') if row.get('Sid') == 'GateImageTransition']
        return {self.image_parameter, *[ledger.text(ledger.mapping(row['Resource']), 'Fn::Sub').split('/')[-1]
                                      for row in transition]}


class HanaSetupFailureTests(CostFixture):
    PROBE_OPERATIONS: ClassVar[tuple[str, ...]] = ('export-credentials', 'describe-instances',
        'describe-spot-instance-requests', 'describe-instance-attribute', 'modify-instance-attribute',
        'start-instances', 'stop-instances', 'put-object', 'get-object')
    clock: TestClock = TestClock()
    workload: Path = Path()
    setup_path: Path = Path()
    book: ledger.CostLedger = ledger.CostLedger(Path())
    calls: list[list[str]] = []
    policy_calls: list[tuple[str, str]] = []
    scaler_calls: list[tuple[str, str]] = []
    instance_reads: list[list[str]] = []
    deleted_stacks: dict[str, tuple[int, list[dict[str, str]]]] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.clock = TestClock()
        self.workload = write_hana_config()
        self.setup_path = self.state / 'hana/setup.json'
        self.book = ledger.CostLedger(self.state)
        self.calls = []
        self.policy_calls = []
        self.scaler_calls = []
        self.instance_reads = []
        self.deleted_stacks = {}
        numeric_settings(self.workload, {'intent_lookup_timeout_s': '15', 'idle_stop_seconds': '10',
                                          'stop_wait_timeout_s': '25', 'setup_idle_wait_timeout_s': '45'})
        self.book.record_inventory(ledger.ResourceInventory(self.clock.now, (), 'fixture setup inventory', True))
        self.book.observe(ledger.InstanceObservation(self.clock.now, 'terminated', 'fixture setup account'))
        original = conf.read_or_repository

        def configuration(name: str) -> conf.ConfigRead:
            return conf.read(self.workload) if name == 'hana_aws' else original(name)

        _ = self.enterContext(mock.patch.object(conf, 'read_or_repository', side_effect=configuration))
        original_path = conf.path

        def configuration_path(name: str | Path) -> Path:
            return self.workload if name == 'hana_aws' else original_path(name)

        _ = self.enterContext(mock.patch.object(conf, 'path', side_effect=configuration_path))
        settings = aws_config.DeploymentSettings('us-east-2', 'fixture-admin', 'fixture-gate', 'image',
            'owner', 'fixture-release', 'us-east-2b', 'vpc', 'subnet', '/dev/xvda', 1, 2, 3, 10, 300, 'fixture-scaler')
        _ = self.enterContext(mock.patch.object(deploy, 'read_deployment_settings', return_value=settings))
        _ = self.enterContext(mock.patch.object(deploy.Deployment, '_check_credentials'))
        _ = self.enterContext(mock.patch.object(deploy.Deployment, '_validate_inputs'))

    def subject(self, fake: FakeEc2, *, waiter_timeout: bool = False, retain_on_delete: bool = False,
                spot_prices: tuple[tuple[str, str], ...] = (('us-east-2b', '0.10'),),
                launch_errors: tuple[str, ...] = (), volume_errors: tuple[str, ...] = (),
                legacy_stack: bool = False) -> deploy.Deployment:
        status = 'CREATE_COMPLETE' if fake.stack else 'CREATE_IN_PROGRESS'
        created = self.clock.now
        stack_id = 'active-stack'
        errors = iter(launch_errors)
        disk_errors = iter(volume_errors)

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                    check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            nonlocal status, created, stack_id
            self.calls.append(list(arguments))
            offset = arguments.index('--region') + 2
            operation = arguments[offset + 1]
            if operation == 'describe-subnets':
                vpc = conf.read(self.workload).text('vpc_id')
                self.assertIn(f'Name=vpc-id,Values={vpc}', arguments)
                return subprocess.CompletedProcess(arguments, 0, json.dumps({'Subnets': [
                    {'SubnetId': 'fixture-subnet-' + suffix, 'AvailabilityZone': 'us-east-2' + suffix,
                     'VpcId': vpc, 'State': 'available', 'DefaultForAz': True} for suffix in ('a', 'c')]}), '')
            if operation == 'describe-spot-price-history':
                if fake.fail == operation:
                    return subprocess.CompletedProcess(arguments, 1, '', 'fixture Spot read failed')
                size = conf.read(self.workload).text('instance_type')
                rows = [{'InstanceType': size, 'AvailabilityZone': zone, 'SpotPrice': price,
                         'Timestamp': datetime.fromtimestamp(self.clock.now, UTC).isoformat()}
                        for zone, price in spot_prices]
                return subprocess.CompletedProcess(arguments, 0, json.dumps({'SpotPriceHistory': rows}), '')
            if operation == 'create-change-set':
                created = self.clock.now
                stack_id = f'active-stack-{len(self.deleted_stacks)}'
                status = 'CREATE_IN_PROGRESS'
            if operation == 'describe-stacks' and fake.stack:
                fake.operations.append(operation)
                stack: dict[str, object] = {'StackStatus': status, 'StackId': stack_id}
                if status == 'CREATE_COMPLETE':
                    outputs = {'ControlBucket': 'control-bucket', 'LaunchTemplate': 'fixture-template',
                               'LaunchTemplateVersion': '1', 'SecurityGroup': 'fixture-group'}
                    if legacy_stack:
                        outputs.update(Instance='fixture-instance', DataVolume='fixture-disk')
                    stack['Outputs'] = [{'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()]
                return subprocess.CompletedProcess(arguments, 0, json.dumps({'Stacks': [stack]}), '')
            if operation == 'run-instances':
                fake.operations.append(operation)
                if fake.raise_once == operation:
                    fake.raise_once = ''
                    raise subprocess.TimeoutExpired('fixture launch', timeout)
                if launch_errors:
                    return subprocess.CompletedProcess(arguments, 1, '', next(errors))
                instance = fake.launch(tagged=True)
                reply = {'Instances': [{'InstanceId': instance, 'SpotInstanceRequestId': fake.boxes[instance].request}]}
                return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
            if operation == 'create-volume':
                fake.operations.append(operation)
                if fake.raise_once == operation:
                    fake.raise_once = ''
                    raise subprocess.TimeoutExpired('fixture volume creation', timeout)
                return subprocess.CompletedProcess(arguments, 1, '', next(disk_errors))
            if operation == 'list-stacks':
                self.assertIn('DELETE_COMPLETE', arguments)
                fake.operations.append(operation)
                stack_name = conf.read(self.workload).text('stack_name')
                summaries = [{'StackName': stack_name, 'StackId': identity, 'StackStatus': 'DELETE_COMPLETE',
                              'CreationTime': datetime.fromtimestamp(at, UTC).isoformat()}
                             for identity, (at, _) in self.deleted_stacks.items()]
                return subprocess.CompletedProcess(arguments, 0, json.dumps({'StackSummaries': summaries}), '')
            if operation == 'describe-stack-events':
                identity = arguments[arguments.index('--stack-name') + 1]
                if identity in self.deleted_stacks:
                    fake.operations.append(operation)
                    return subprocess.CompletedProcess(arguments, 0, json.dumps({
                        'StackEvents': self.deleted_stacks[identity][1]}), '')
            if 'stack-create-complete' in arguments:
                self.clock.advance(1)
                if waiter_timeout:
                    raise subprocess.TimeoutExpired('fixture stack create waiter', timeout)
                if fake.fail_wait == 'stack-create-complete':
                    status = 'ROLLBACK_COMPLETE'
                    fake.events.append({'LogicalResourceId': 'Instance', 'ResourceType': 'AWS::EC2::Instance',
                        'ResourceStatus': 'CREATE_FAILED', 'ResourceStatusReason': 'No capacity in this zone',
                        'Timestamp': datetime.fromtimestamp(self.clock.now, UTC).isoformat()})
                else:
                    status = 'CREATE_COMPLETE'
            if operation == 'delete-stack' and fake.stack:
                if retain_on_delete:
                    fake.create_retained()
                fake.roll_back()
                self.deleted_stacks[stack_id] = (created, fake.events.copy())
            return fake(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)

        def api(settings: aws_config.DeploymentSettings) -> aws_cli.AwsCli:
            return aws_cli.AwsCli(settings.profile, settings.region, settings.timeout_s, execute)

        _ = self.enterContext(mock.patch.object(deploy.Deployment, '_api', side_effect=api))
        return deploy.Deployment(self.settings, self.state, self.root / 'output', clock=self.clock, notify=lambda _event: True)

    def write_leftover(self, subject: deploy.Deployment, *, instance: str = '') -> int:
        limits = hana_spend.read_settings(conf.read(self.workload))
        reservation = hana_spend.HanaSpending(self.book, self.settings, limits).reserve_setup(
            self.clock.now, self.clock.now + limits.permit_max_seconds)
        assert isinstance(reservation, hana_spend.StartReserved)
        generation = reservation.exposure.generation
        self.setup_path.parent.mkdir()
        saved = {'version': 1, 'deployment': HANA_TAG[1], 'stack': conf.read(self.workload).text('stack_name'),
                 'region': limits.region, 'requested': True, 'generation': generation,
                 'template_sha256': 'old-template', 'parameters_sha256': 'old-parameters'}
        if instance:
            saved['instance'] = instance
        write_text_atomic(self.setup_path, json.dumps(saved))
        self.clock.advance(limits.permit_max_seconds + 1)
        _ = subject.prepare_hana()
        return generation

    def advance_pause(self, seconds: float) -> None:
        self.clock.advance(int(seconds))

    def assert_spot_price_refusal(self, subject: deploy.Deployment) -> str:
        _ = subject.prepare_hana()
        with redirect_stdout(StringIO()), self.assertRaises(deploy.DeploymentRefused) as failure:
            _ = subject.hana_setup(approve_resources=True)
        subnets, call = self.calls
        self.assertIn('describe-subnets', subnets)
        self.assertIn('describe-spot-price-history', call)
        self.assertIn('us-east-2', call)
        self.assertIn('--instance-types', call)
        self.assertIn(conf.read(self.workload).text('instance_type'), call)
        self.assertNotIn('--availability-zone', call)
        self.assertEqual(self.book.read_snapshot().hana_compute, [])
        return str(failure.exception)

    def test_hana_setup_refuses_price_above_ceiling_and_suggests_a_new_ceiling(self) -> None:
        text_setting(self.workload, 'spot_max_usd_per_hour', '0.15')
        text_setting(self.workload, 'spot_ceiling_margin', '0.27')
        subject = self.subject(FakeEc2(), spot_prices=(
            ('us-east-2a', '0.2010'), ('us-east-2b', '0.1928'), ('us-east-2c', '0.1828')))
        message = self.assert_spot_price_refusal(subject)
        self.assertIn('No Hana Spot candidate meets the 0.15 USD/h ceiling.', message)
        size = conf.read(self.workload).text('instance_type')
        self.assertIn(f'The cheapest candidate is {size} in us-east-2c at 0.1828 USD/h.', message)
        self.assertEqual(message.splitlines()[-3:], [
            '1. Run /tune hana_aws spot_max_usd_per_hour 0.24',
            '2. Run deploy.py prepare --workload hana',
            '3. Run deploy.py hana-setup --approve-resources',
        ])

    def test_hana_setup_suggests_tuning_the_effective_host_ceiling(self) -> None:
        key = 'spot_max_usd_per_hour.hana-test'
        text_setting(self.workload, key, '0.15')
        subject = self.subject(FakeEc2(), spot_prices=(('us-east-2b', '0.18'),))
        with mock.patch('socket.gethostname', return_value='hana-test.example'):
            message = self.assert_spot_price_refusal(subject)
            _ = conf.editable_line(self.workload, key)
        self.assertIn('No Hana Spot candidate meets the 0.15 USD/h ceiling.', message)
        self.assertIn(f'1. Run /tune hana_aws {key} 0.23', message.splitlines())

    def test_hana_setup_reports_another_host_override_that_blocks_tune(self) -> None:
        key = 'spot_max_usd_per_hour'
        text_setting(self.workload, key, '0.15')
        text_setting(self.workload, f'{key}.other-host', '0.20')
        subject = self.subject(FakeEc2(), spot_prices=(('us-east-2b', '0.18'),))
        with mock.patch('socket.gethostname', return_value='hana-test.example'):
            with self.assertRaises(conf.Unusable) as refusal:
                _ = conf.editable_line(self.workload, key)
            message = self.assert_spot_price_refusal(subject)
        self.assertIn('The suggested ceiling is 0.23 USD/hour.', message.splitlines())
        self.assertIn(str(refusal.exception), message.splitlines())
        self.assertNotIn('/tune hana_aws', message)

    def test_hana_setup_and_the_configuration_validator_refuse_a_zero_margin(self) -> None:
        key = 'spot_ceiling_margin'
        source = self.workload.read_text()
        conf.check(self.workload, key, '0.25')
        with self.assertRaises(conf.Unusable):
            conf.check(self.workload, key, '0')
        _ = self.workload.write_text(source.replace('spot_ceiling_margin=0.25', 'spot_ceiling_margin=0'))
        subject = self.subject(FakeEc2())
        _ = subject.prepare_hana()
        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'positive spot_ceiling_margin'):
            _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.book.read_snapshot().hana_compute, [])

    def test_hana_setup_continues_at_the_price_ceiling(self) -> None:
        text_setting(self.workload, 'spot_max_usd_per_hour', '0.15')
        fake = FakeEc2(fail_wait='stack-create-complete')
        subject = self.subject(fake, spot_prices=(('us-east-2b', '0.15'),))
        _ = subject.prepare_hana()

        output = StringIO()
        with redirect_stdout(output), self.assertRaises(deploy.HanaCreateRolledBack):
            _ = subject.hana_setup(approve_resources=True)
        self.assertIn('create-change-set', fake.operations)
        self.assertIn('updating the separate Hana stack', output.getvalue())

    def test_hana_setup_refuses_when_no_eligible_subnet_has_a_price(self) -> None:
        subject = self.subject(FakeEc2(), spot_prices=(('us-east-2d', '0.14'),))
        message = self.assert_spot_price_refusal(subject)
        self.assertIn('AWS lists no Spot prices for Hana instance types in the eligible subnets of us-east-2.', message)

    def test_hana_setup_refuses_when_the_price_read_fails(self) -> None:
        subject = self.subject(FakeEc2(fail='describe-spot-price-history'))
        message = self.assert_spot_price_refusal(subject)
        self.assertIn('Could not read Spot prices for Hana instance types in us-east-2.', message)

    def test_hana_setup_keeps_the_stack_after_all_launches_fail_and_reuses_it(self) -> None:
        fake = FakeEc2()
        failures = ('aws: [ERROR]: An error occurred (SpotMaxPriceTooLow) when calling the RunInstances operation: price increased',
                    'no Spot capacity in this zone')
        subject = self.subject(fake, spot_prices=(('us-east-2b', '0.14'), ('us-east-2c', '0.10')),
                               launch_errors=failures * 2)
        _ = subject.prepare_hana()
        for run in (1, 2):
            with self.subTest(run=run), redirect_stdout(StringIO()) as output, self.assertRaises(deploy.DeploymentRefused) as raised:
                _ = subject.hana_setup(approve_resources=True)
            self.assertIn('No Hana Spot candidate launched.', str(raised.exception))
            self.assertIn('us-east-2b: attempt 1 failed:', str(raised.exception))
            self.assertIn('us-east-2c: attempt 2 failed:', str(raised.exception))
            self.assertEqual(sum(line.startswith('attempt ') for line in output.getvalue().splitlines()), 4)
            self.assertEqual(fake.operations.count('create-change-set'), 1)
            self.assertNotIn('delete-stack', fake.operations)
            self.assertNotIn('pending_launch', remote.read_record(self.setup_path))
            self.assertTrue(fake.stack)
        self.assertEqual(len(self.book.read_snapshot().hana_compute), 2)
        self.assertTrue(all(isinstance(row.state, ledger.HanaExposureClosed) for row in self.book.read_snapshot().hana_compute))

    def test_hana_down_completes_after_the_first_launch_is_denied(self) -> None:
        fake = FakeEc2()
        subject = self.subject(fake, spot_prices=(('us-east-2b', '0.12'), ('us-east-2c', '0.10')), launch_errors=(
            'aws: [ERROR]: An error occurred (UnauthorizedOperation) when calling the RunInstances operation: denied',))
        _ = subject.prepare_hana()
        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'attempt 1 failed: .*denied'):
            _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(fake.operations.count('run-instances'), 1)
        self.assertNotIn('delete-stack', fake.operations)
        self.assertNotIn('pending_launch', remote.read_record(self.setup_path))
        with redirect_stdout(StringIO()):
            self.assertTrue(subject.hana_down(approve_destroy=True))
        self.assertEqual(fake.leftovers(), ([], [], [], [], False))
        self.assertFalse(self.setup_path.exists())

    def test_hana_down_completes_after_the_first_launch_hits_a_full_subnet(self) -> None:
        fake = FakeEc2()
        subject = self.subject(fake, spot_prices=(('us-east-2b', '0.12'), ('us-east-2c', '0.10')), launch_errors=(
            'aws: [ERROR]: An error occurred (InsufficientFreeAddressesInSubnet) '
            + 'when calling the RunInstances operation: subnet full',))
        _ = subject.prepare_hana()
        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'InsufficientFreeAddressesInSubnet'):
            _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(fake.operations.count('run-instances'), 1)
        self.assertNotIn('pending_launch', remote.read_record(self.setup_path))
        with redirect_stdout(StringIO()):
            self.assertTrue(subject.hana_down(approve_destroy=True))
        self.assertEqual(fake.leftovers(), ([], [], [], [], False))
        self.assertFalse(self.setup_path.exists())

    def test_hana_setup_keeps_the_launch_token_if_a_retry_after_timeout_is_refused(self) -> None:
        fake = FakeEc2(raise_once='run-instances')
        failures = (
            'no Spot capacity',
            'aws: [ERROR]: An error occurred (InsufficientFreeAddressesInSubnet) '
            + 'when calling the RunInstances operation: subnet full',
        )
        subject = self.subject(fake, launch_errors=failures)
        _ = subject.prepare_hana()
        with redirect_stdout(StringIO()), self.assertRaises(subprocess.TimeoutExpired):
            _ = subject.hana_setup(approve_resources=True)
        pending = ledger.mapping(remote.read_record(self.setup_path)['pending_launch'])
        for error in ('no Spot capacity', 'InsufficientFreeAddressesInSubnet'):
            with self.subTest(error=error):
                with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, error):
                    _ = subject.hana_setup(approve_resources=True)
                self.assertEqual(remote.read_record(self.setup_path)['pending_launch'], pending)
        calls = [call for call in self.calls if 'run-instances' in call]
        self.assertEqual([call[call.index('--client-token') + 1] for call in calls], [pending['token']] * 3)
        self.assertEqual(fake.operations.count('create-change-set'), 1)

    def test_hana_setup_keeps_the_launch_token_after_an_internal_error(self) -> None:
        fake = FakeEc2()
        subject = self.subject(fake,
            launch_errors=('An error occurred (InternalFailure) when calling the RunInstances operation: unknown outcome',))
        _ = subject.prepare_hana()
        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'unknown outcome'):
            _ = subject.hana_setup(approve_resources=True)
        pending = ledger.mapping(remote.read_record(self.setup_path)['pending_launch'])
        launch, = [call for call in self.calls if 'run-instances' in call]
        self.assertEqual(pending['token'], launch[launch.index('--client-token') + 1])

    def test_hana_down_completes_after_the_first_disk_creation_is_denied(self) -> None:
        fake = FakeEc2()
        subject = self.subject(fake, volume_errors=(
            'aws: [ERROR]: An error occurred (UnauthorizedOperation) when calling the CreateVolume operation: denied',))
        _ = subject.prepare_hana()
        with redirect_stdout(StringIO()), self.assertRaisesRegex(aws_cli.AwsUnavailable, 'denied'):
            _ = subject.hana_setup(approve_resources=True)
        saved = remote.read_record(self.setup_path)
        self.assertIn('instance', saved)
        self.assertNotIn('pending_volume', saved)
        with redirect_stdout(StringIO()):
            self.assertTrue(subject.hana_down(approve_destroy=True))
        self.assertEqual(fake.leftovers(), ([], [], [], [], False))
        self.assertFalse(self.setup_path.exists())

    def test_hana_setup_keeps_the_disk_intent_if_a_retry_after_timeout_is_denied(self) -> None:
        fake = FakeEc2(raise_once='create-volume')
        subject = self.subject(fake, volume_errors=(
            'aws: [ERROR]: An error occurred (UnauthorizedOperation) when calling the CreateVolume operation: denied',))
        workload = conf.read(self.workload)
        fake.image = workload.text('ami_id')
        _ = hana_image.save(self.state, hana_image.RegisteredImage('fixture', fake.image,
            workload.text('ami_owner_id'), workload.text('vpc_id'), workload.text('subnet_id'),
            hana_render.guest_digest(self.workload)))
        _ = subject.prepare_hana()
        with redirect_stdout(StringIO()), self.assertRaises(subprocess.TimeoutExpired):
            _ = subject.hana_setup(approve_resources=True)
        token = ledger.text(remote.read_record(self.setup_path), 'pending_volume')
        with redirect_stdout(StringIO()), self.assertRaisesRegex(aws_cli.AwsUnavailable, 'denied'):
            _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(remote.read_record(self.setup_path)['pending_volume'], token)
        calls = [call for call in self.calls if 'create-volume' in call]
        self.assertEqual([call[call.index('--client-token') + 1] for call in calls], [token] * 2)

    def test_hana_create_commands_use_one_cli_attempt(self) -> None:
        execute = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{}', ''))
        subject = deploy.Deployment(self.settings, self.state, self.root / 'output', execute=execute)
        settings, _, _ = subject._hana_inputs()  # pyright: ignore[reportPrivateUsage]
        api = subject._api(settings)  # pyright: ignore[reportPrivateUsage]
        environment = {'AWS_MAX_ATTEMPTS': '3'}
        with mock.patch.object(subject.admin, 'environment', return_value=environment):
            _ = api.call('run-instances')
            _ = api.call('create-volume')
            _ = api.call('describe-volumes')
        self.assertEqual([call.kwargs['env']['AWS_MAX_ATTEMPTS'] for call in execute.call_args_list], ['1', '1', '3'])

    def test_hana_down_deletes_a_direct_volume_before_it_deletes_the_stack(self) -> None:
        fake = FakeEc2(stack=True)
        instance = fake.launch(tagged=True)
        request = fake.boxes[instance].request
        fake.requests[request].tags.clear()
        fake.boxes[instance].tags.clear()
        fake.disks['fixture-data'] = Disk('available', {})
        subject = self.subject(fake)
        _ = self.write_leftover(subject)
        saved = remote.read_record(self.setup_path)
        saved.update(version=2, instance=instance, spot_request=request, data_volume='fixture-data')
        write_text_atomic(self.setup_path, json.dumps(saved))
        with redirect_stdout(StringIO()):
            self.assertTrue(subject.hana_down(approve_destroy=True))
        actions = ('cancel-spot-instance-requests', 'terminate-instances', 'wait instance-terminated',
                   'delete-volume', 'wait volume-deleted', 'delete-stack')
        positions = [fake.operations.index(action) for action in actions]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(fake.leftovers(), ([], [], [], [], False))
        self.assertFalse(self.setup_path.exists())
        self.assertIsInstance(self.book.read_snapshot().hana_storage[0].state, ledger.HanaExposureClosed)

    def test_hana_down_waits_for_a_disk_hidden_after_aws_accepts_creation(self) -> None:
        fake = FakeEc2(stack=True, hidden_volume_reads=1)
        instance = fake.launch(tagged=True)
        token = 'fixture-volume-token'
        fake.disks['fixture-data'] = Disk('available', {'HanaDeployment': HANA_TAG[1], 'HanaVolumeToken': token})
        subject = self.subject(fake)
        _ = self.write_leftover(subject)
        saved = remote.read_record(self.setup_path)
        saved.update(version=2, instance=instance, spot_request=fake.boxes[instance].request, pending_volume=token)
        write_text_atomic(self.setup_path, json.dumps(saved))
        subject.pause = self.advance_pause
        with redirect_stdout(StringIO()), mock.patch.object(time, 'monotonic', self.clock):
            self.assertTrue(subject.hana_down(approve_destroy=True))
        lookups = [call for call in self.calls if 'describe-volumes' in call]
        self.assertEqual(len(lookups), 2)
        self.assertTrue(all(f'Name=tag:HanaVolumeToken,Values={token}' in call for call in lookups))
        self.assertLess(fake.operations.index('delete-volume'), fake.operations.index('delete-stack'))
        self.assertEqual(fake.leftovers(), ([], [], [], [], False))
        self.assertFalse(self.setup_path.exists())
        self.assertIsInstance(self.book.read_snapshot().hana_storage[0].state, ledger.HanaExposureClosed)

    def test_hana_down_resolves_a_pending_launch_before_teardown(self) -> None:
        fake = FakeEc2(stack=True, hidden_launch_reads=1)
        instance = fake.launch(tagged=False)
        request = fake.boxes[instance].request
        fake.requests[request].tags.clear()
        fake.launch_tokens[instance] = 'fixture-launch-token'
        subject = self.subject(fake)
        _ = self.write_leftover(subject)
        saved = remote.read_record(self.setup_path)
        saved.update(version=2, pending_launch={'instance_type': 'm7a.4xlarge', 'zone': 'us-east-2b',
                                                'subnet': 'fixture-subnet', 'token': fake.launch_tokens[instance]})
        write_text_atomic(self.setup_path, json.dumps(saved))
        subject.pause = self.advance_pause
        with redirect_stdout(StringIO()), mock.patch.object(time, 'monotonic', self.clock):
            self.assertTrue(subject.hana_down(approve_destroy=True))
        self.assertEqual(sum('Name=client-token,Values=fixture-launch-token' in call for call in self.calls), 2)
        actions = ('cancel-spot-instance-requests', 'terminate-instances', 'delete-stack')
        positions = [fake.operations.index(action) for action in actions]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(fake.leftovers(), ([], [], [], [], False))

    def test_hana_down_keeps_the_stack_and_charges_when_an_intent_stays_unresolved(self) -> None:
        fake = FakeEc2(stack=True)
        subject = self.subject(fake)
        _ = self.write_leftover(subject)
        saved = remote.read_record(self.setup_path)
        evidence = self.book.read_snapshot()
        subject.pause = self.advance_pause
        intent = {'instance_type': 'm7a.4xlarge', 'zone': 'us-east-2b', 'subnet': 'fixture-subnet', 'token': 'missing-launch'}
        for kind, pending in (('launch', {'pending_launch': intent}), ('disk creation', {'pending_volume': 'missing-disk'})):
            with self.subTest(kind=kind):
                record = {**saved, **pending, 'version': 2}
                write_text_atomic(self.setup_path, json.dumps(record))
                with redirect_stdout(StringIO()), mock.patch.object(time, 'monotonic', self.clock), \
                        self.assertRaisesRegex(deploy.DeploymentRefused, f'AWS did not confirm the pending Hana {kind}'):
                    _ = subject.hana_down(approve_destroy=True)
                self.assertNotIn('delete-stack', fake.operations)
                self.assertTrue(fake.stack)
                self.assertEqual(remote.read_record(self.setup_path), record)
                self.assertEqual(self.book.read_snapshot(), evidence)

    def test_hana_down_recovers_the_request_and_disk_from_a_saved_instance(self) -> None:
        fake = FakeEc2(stack=True)
        instance = fake.launch(tagged=False)
        fake.disks['fixture-data'] = Disk('available', {'HanaDeployment': HANA_TAG[1], 'HanaData': 'true'})
        subject = self.subject(fake)
        _ = self.write_leftover(subject)
        saved = remote.read_record(self.setup_path)
        saved.update(version=2, instance=instance)
        write_text_atomic(self.setup_path, json.dumps(saved))
        with redirect_stdout(StringIO()):
            self.assertTrue(subject.hana_down(approve_destroy=True))
        self.assertIn('cancel-spot-instance-requests', fake.operations)
        self.assertIn('delete-volume', fake.operations)
        self.assertEqual(fake.leftovers(), ([], [], [], [], False))

    def test_hana_down_reports_missing_direct_ids_and_continues(self) -> None:
        fake = FakeEc2(stack=True)
        subject = self.subject(fake)
        _ = subject.prepare_hana()
        with redirect_stdout(StringIO()) as output:
            self.assertTrue(subject.hana_down(approve_destroy=True))
        for missing in ('Spot request', 'instance', 'data disk'):
            self.assertIn(f'no saved {missing} ID', output.getvalue())
        self.assertIn('delete-stack', fake.operations)
        self.assertFalse(self.setup_path.exists())

    def test_hana_setup_refuses_a_stack_with_legacy_instance_outputs(self) -> None:
        fake = FakeEc2(stack=True)
        subject = self.subject(fake, legacy_stack=True)
        _ = self.write_leftover(subject)
        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'down --workload hana --approve-destroy first'):
            _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(fake.operations, ['describe-stacks'])

    def test_hana_setup_refuses_a_legacy_stack_instance_even_without_its_output(self) -> None:
        fake = FakeEc2(stack=True, resources={'Instance': ('AWS::EC2::Instance', 'fixture-instance')})
        subject = self.subject(fake)
        _ = self.write_leftover(subject)
        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'down --workload hana --approve-destroy first'):
            _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(fake.operations, ['describe-stacks', 'describe-stack-resources'])

    @staticmethod
    def stack_owned_hana_template() -> dict[str, object]:
        template = ledger.mapping(cast(object, json.loads(deploy.HANA_TEMPLATE.read_bytes())))
        parameters = ledger.mapping(template['Parameters'])
        parameters.update({key: {'Type': 'String'} for key in ('InstanceType', 'AvailabilityZone', 'SubnetId')})
        parameters['DataGiB'] = {'Type': 'Number', 'MinValue': 1}
        resources = ledger.mapping(template['Resources'])
        tags = [{'Key': 'HanaDeployment', 'Value': {'Ref': 'DeploymentId'}}, {'Key': 'Workload', 'Value': 'hana'}]
        resources['Instance'] = {'Type': 'AWS::EC2::Instance', 'DeletionPolicy': 'Retain', 'UpdateReplacePolicy': 'Retain',
            'Properties': {'AvailabilityZone': {'Ref': 'AvailabilityZone'}, 'Tags': tags, 'LaunchTemplate': {
                'LaunchTemplateId': {'Ref': 'LaunchTemplate'},
                'Version': {'Fn::GetAtt': ['LaunchTemplate', 'LatestVersionNumber']}}}}
        resources['DataVolume'] = {'Type': 'AWS::EC2::Volume', 'DeletionPolicy': 'Retain', 'UpdateReplacePolicy': 'Retain',
            'Properties': {'AvailabilityZone': {'Ref': 'AvailabilityZone'}, 'Size': {'Ref': 'DataGiB'},
                           'VolumeType': 'gp3', 'Encrypted': True, 'Tags': tags}}
        resources['DataAttachment'] = {'Type': 'AWS::EC2::VolumeAttachment', 'Properties': {
            'InstanceId': {'Ref': 'Instance'}, 'VolumeId': {'Ref': 'DataVolume'}, 'Device': '/dev/sdf'}}
        launch = ledger.mapping(ledger.mapping(resources['LaunchTemplate'])['Properties'])
        ledger.mapping(launch['LaunchTemplateData']).update(InstanceType={'Ref': 'InstanceType'},
            NetworkInterfaces=[{'DeviceIndex': 0, 'SubnetId': {'Ref': 'SubnetId'},
                                'AssociatePublicIpAddress': True, 'Groups': [{'Ref': 'SecurityGroup'}]}])
        policy = ledger.mapping(ledger.mapping(resources['ScalerPolicy'])['Properties'])
        for statement in ledger.rows(ledger.mapping(policy['PolicyDocument']), 'Statement'):
            if statement.get('Action') == ['ec2:ModifyInstanceAttribute']:
                ledger.mapping(ledger.mapping(statement['Condition'])['StringEquals'])['ec2:Attribute'] = 'userData'
        template['Outputs'] = {key: {'Value': {'Ref': key}} for key in ('Instance', 'DataVolume', 'ControlBucket', 'LaunchTemplate')}
        return template

    def policy_update_fixture(self, changes: list[dict[str, object]], *, fail_update: bool = False,
                              change_status: str = 'CREATE_COMPLETE', next_token: str = '',
                              execution: Literal['accepted', 'refused', 'timeout', 'unknown'] = 'accepted',
                              stack_disappears: bool = False, changed_scaler_user: str = '',
                              live_state: ledger.InstanceState = 'stopped', execution_error: str = '',
                              admin_spot_error: str = '',
                              scaler_faults: Mapping[tuple[str, str], str] | None = None,
                              bad_readback: bool = False, volume_device: str = '/dev/sdf',
                              stack_owned: bool = False, current_guest_image: bool = False
                              ) -> tuple[deploy.Deployment, dict[str, str]]:
        scaler_faults = scaler_faults or {}
        credentials = self.root / '.config/aws-build/credentials'
        service_settings = self.root / 'service-aws-target.conf'
        _ = service_settings.write_text('credentials_profile=fixture-service-scaler\n')
        original_path = conf.path

        def configuration_path(name: str | Path) -> Path:
            return service_settings if name == 'aws_target' else original_path(name)

        _ = self.enterContext(mock.patch.object(conf, 'path', side_effect=configuration_path))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            'AWS_SHARED_CREDENTIALS_FILE': str(self.root / 'caller-credentials'), 'AWS_ACCESS_KEY_ID': 'fixture-admin-key',
            'AWS_SECRET_ACCESS_KEY': 'fixture-admin-secret', 'AWS_SESSION_TOKEN': 'fixture-admin-token',
            'AWS_PROFILE': 'fixture-admin', 'AWS_MAX_ATTEMPTS': '5', 'AWS_CLI_ERROR_FORMAT': 'json'})))

        updated = False
        object_body = b''

        def scaler(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                   check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            nonlocal object_body
            _ = capture_output, text, timeout, check
            self.assertEqual({key: value for key, value in (env or {}).items() if key.startswith('AWS_')}, {
                'AWS_SHARED_CREDENTIALS_FILE': str(credentials), 'AWS_CONFIG_FILE': os.devnull,
                'AWS_EC2_METADATA_DISABLED': 'true'})
            self.assertEqual(arguments[:5], ['aws', '--profile', 'fixture-service-scaler', '--region', 'us-east-2'])
            service, operation = arguments[5:7]
            phase = 'after' if updated else 'before'
            self.scaler_calls.append((phase, operation))
            reply: dict[str, object] = {}
            dry_run = operation in ('modify-instance-attribute', 'start-instances', 'stop-instances')
            self.assertEqual('--dry-run' in arguments, dry_run)
            self.policy_calls.append((service, operation + (' --dry-run' if dry_run else '')))
            if operation == 'export-credentials':
                self.assertEqual(service, 'configure')
                reply = {'AccessKeyId': 'fixture-access', 'SecretAccessKey': 'fixture-secret'}
            elif operation in ('put-object', 'get-object'):
                self.assertEqual(service, 's3api')
                self.assertEqual(arguments[7:11], ['--bucket', 'fixture-bucket',
                    '--key', f"control/{saved['deployment']}/setup-probe.json"])
                if operation == 'put-object':
                    object_body = Path(arguments[arguments.index('--body') + 1]).read_bytes()
                    self.assertTrue(object_body)
                else:
                    _ = Path(arguments[-3]).write_bytes(b'wrong' if bad_readback else object_body)
            else:
                self.assertEqual(service, 'ec2')
                if operation == 'describe-spot-instance-requests':
                    self.assertEqual(arguments[7:9], ['--spot-instance-request-ids', 'fixture-spot-request'])
                elif operation in ('describe-instance-attribute', 'modify-instance-attribute'):
                    self.assertEqual(arguments[7:11], ['--instance-id', 'fixture-box', '--attribute', 'userData'])
                    if operation == 'describe-instance-attribute':
                        reply = {'UserData': {'Value': 'Zml4dHVyZQ=='}}
                    else:
                        value = arguments[arguments.index('--value') + 1]
                        self.assertTrue(value.startswith('file://'))
                        original = Path(value.removeprefix('file://')).read_text()
                        self.assertEqual(original, '' if (phase, 'describe-instance-attribute') in scaler_faults else 'Zml4dHVyZQ==')
                else:
                    self.assertEqual(arguments[7:9], ['--instance-ids', 'fixture-box'])
            code = scaler_faults.get((phase, operation), 'DryRunOperation' if dry_run else '')
            if code:
                action = ''.join(word.title() for word in operation.split('-'))
                return subprocess.CompletedProcess(arguments, 1, '',
                    f'aws: [ERROR]: An error occurred ({code}) when calling the {action} operation: private diagnostic')
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

        subject = deploy.Deployment(self.settings, self.state, self.root / 'output',
                                    execute=scaler, clock=self.clock, notify=lambda _event: True)
        if current_guest_image:
            text_setting(self.workload, 'enabled', 'on')
        # Exercise the policy update path separately from the existing box preflight.
        _ = self.enterContext(mock.patch.object(subject, '_hana_root_replacement', return_value=False))
        prepared = subject.prepare_hana()
        outputs = {'ControlBucket': 'fixture-bucket', 'LaunchTemplate': 'fixture-template',
                   'LaunchTemplateVersion': '3', 'SecurityGroup': 'fixture-group'}
        instance, volume = 'fixture-box', 'fixture-volume'
        saved = remote.read_record(self.setup_path)
        saved.update(requested=True, generation=1, instance=instance, data_volume=volume,
                     instance_type=prepared.parameters['InstanceType'], bucket=outputs['ControlBucket'],
                     launch_template='LaunchTemplateId=fixture-template,Version=3', security_group=outputs['SecurityGroup'],
                     spot_request='fixture-spot-request',
                     template_sha256=sha256(b'previous fixture template').hexdigest(),
                     parameters_sha256=sha256(json.dumps(prepared.parameters, sort_keys=True).encode()).hexdigest())
        current = ledger.mapping(cast(object, json.loads(deploy.HANA_TEMPLATE.read_bytes())))
        template = self.stack_owned_hana_template() if stack_owned else current
        live_parameters = {key: prepared.parameters[key] for key in ledger.mapping(template['Parameters'])}
        if stack_owned:
            outputs = {'ControlBucket': 'fixture-bucket', 'LaunchTemplate': 'fixture-template',
                       'Instance': instance, 'DataVolume': volume}
            saved.update(version=1, parameters_sha256=sha256(json.dumps(live_parameters, sort_keys=True).encode()).hexdigest())
            for key in ('launch_template', 'security_group', 'instance_type'):
                del saved[key]
            shutil.rmtree(subject.output / 'hana')
        if current_guest_image:
            saved['guest_digest'] = hana_render.guest_digest(self.workload).value
        pending_template = template
        write_text_atomic(self.setup_path, json.dumps(saved))
        hana_box.HanaController(self.state, self.settings, self.workload, hana_box.ProviderNotInstalled(),
            clock=self.clock).register(hana_box.DeploymentIdentity(ledger.text(saved, 'deployment'), instance,
                ledger.text(saved, 'spot_request'), ledger.text(saved, 'region')))
        stack_reads = 0
        _ = self.enterContext(mock.patch.object(subject.admin, 'environment', side_effect=lambda: dict(os.environ)))

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                    check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            nonlocal stack_reads, updated, pending_template, template
            _ = capture_output, text, check
            offset = arguments.index('--region') + 2
            service, operation = arguments[offset:offset + 2]
            self.policy_calls.append((service, operation + (' ' + arguments[offset + 2] if operation == 'wait' else '')))
            if service == 'ec2' and operation == 'describe-instances':
                self.instance_reads.append(list(arguments))
                self.assertEqual(arguments[offset + 2:offset + 4], ['--instance-ids', instance])
                reply = {'Reservations': [{'Instances': [{'InstanceId': instance,
                    'LaunchTime': 'fixture-launch', 'State': {'Name': live_state}, 'InstanceLifecycle': 'spot',
                    'BlockDeviceMappings': [{'DeviceName': '/dev/sdf', 'Ebs': {
                        'VolumeId': volume, 'DeleteOnTermination': False}}],
                    'SpotInstanceRequestId': saved['spot_request']}]}]}
                return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
            if service == 'ec2' and operation == 'describe-volumes':
                self.assertEqual(arguments[offset + 2:offset + 4], ['--volume-ids', volume])
                reply = {'Volumes': [{'VolumeId': volume, 'Attachments': [
                    {'InstanceId': instance, 'Device': volume_device, 'State': 'attached',
                     'AttachTime': '2026-01-15T11:00:00Z', 'DeleteOnTermination': False}]}]}
                return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
            if admin_spot_error and (service, operation, arguments[offset + 2]) == ('ec2', 'wait', 'instance-exists'):
                return subprocess.CompletedProcess(arguments, 0, '{}', '')
            if service == 'ec2' and operation == 'describe-spot-instance-requests' and admin_spot_error:
                self.assertIn(saved['spot_request'], arguments)
                assert env is not None
                self.assertEqual(env['AWS_CLI_ERROR_FORMAT'], 'json')
                error_format = env['AWS_CLI_ERROR_FORMAT']
                if '--cli-error-format' in arguments:
                    error_format = arguments[arguments.index('--cli-error-format') + 1]
                stderr = (admin_spot_error if error_format == 'legacy' else
                          json.dumps({'Code': 'InvalidSpotInstanceRequestID.NotFound', 'Message': admin_spot_error}))
                return subprocess.CompletedProcess(arguments, 1, '', stderr)
            self.assertEqual(service, 'cloudformation')
            if operation == 'describe-stack-resources':
                return subprocess.CompletedProcess(arguments, 0, '{"StackResources":[]}', '')
            self.calls.append(list(arguments))
            reply: dict[str, object] = {}
            if operation == 'describe-stacks':
                stack_reads += 1
                if stack_disappears and stack_reads == 2:
                    return subprocess.CompletedProcess(arguments, 1, '', 'ValidationError: stack does not exist')
                reply = {'Stacks': [{'StackId': 'fixture-stack-id', 'StackStatus': 'CREATE_COMPLETE',
                    'Outputs': [{'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()],
                    'Parameters': [{'ParameterKey': key, 'ParameterValue': value} for key, value in live_parameters.items()]}]}
            elif operation == 'get-template' and stack_owned:
                self.assertEqual(arguments[offset + 2:offset + 6], ['--stack-name', 'fixture-stack-id', '--template-stage', 'Original'])
                reply = {'TemplateBody': template if updated else json.dumps(template)}
            elif operation == 'create-change-set' and stack_owned:
                self.assertEqual(arguments[arguments.index('--change-set-type') + 1], 'UPDATE')
                self.assertEqual(arguments[arguments.index('--stack-name') + 1], 'fixture-stack-id')
                values = cast(object, json.loads(arguments[arguments.index('--parameters') + 1]))
                self.assertCountEqual(ledger.rows({'parameters': values}, 'parameters'),
                    [{'ParameterKey': key, 'UsePreviousValue': True} for key in live_parameters])
                body = arguments[arguments.index('--template-body') + 1]
                pending_template = ledger.mapping(cast(object, json.loads(Path(body.removeprefix('file://')).read_bytes())))
                self.assertEqual(pending_template, {**template, 'Resources': {
                    **ledger.mapping(template['Resources']), 'ScalerPolicy': ledger.mapping(current['Resources'])['ScalerPolicy']}})
            elif operation == 'describe-change-set':
                effective = dict(live_parameters)
                if changed_scaler_user:
                    effective['ScalerUser'] = changed_scaler_user
                reply = {'Status': change_status, 'Changes': [{'ResourceChange': change} for change in changes],
                         'Parameters': [{'ParameterKey': key, 'ParameterValue': value} for key, value in effective.items()]}
                if stack_owned and not changed_scaler_user:
                    reply['Parameters'] = [{'ParameterKey': key, 'UsePreviousValue': True} for key in live_parameters]
                if change_status == 'FAILED':
                    reply['StatusReason'] = "The submitted information didn't contain changes."
                if next_token:
                    reply['NextToken'] = next_token
            elif operation == 'execute-change-set':
                self.assertIsNotNone(env)
                assert env is not None
                self.assertEqual(env['AWS_MAX_ATTEMPTS'], '1')
                if execution_error:
                    return subprocess.CompletedProcess(arguments, 1, '', execution_error)
                if execution == 'refused':
                    return subprocess.CompletedProcess(arguments, 1, '',
                        'An error occurred (AccessDenied) when calling the ExecuteChangeSet operation '
                        + '(reached max retries: 0): Fixture refusal')
                if execution == 'timeout':
                    raise subprocess.TimeoutExpired('fixture execution', timeout)
                if execution == 'unknown':
                    return subprocess.CompletedProcess(arguments, 0, '[]', '')
            elif operation == 'wait' and arguments[offset + 2] == 'stack-update-complete':
                self.assertEqual(remote.read_record(self.setup_path), saved)
                self.assertEqual(self.scaler_calls, [('before', action) for action in self.PROBE_OPERATIONS])
                if fail_update:
                    return subprocess.CompletedProcess(arguments, 1, '', 'fixture update failed')
                template = pending_template
                updated = True
            elif operation not in ('create-change-set', 'execute-change-set', 'delete-change-set', 'wait'):
                raise AssertionError('Unexpected CloudFormation operation: ' + operation)
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

        if admin_spot_error:
            _ = self.enterContext(mock.patch.object(aws_cli, 'subprocess', SimpleNamespace(run=execute)))

        def admin_api(settings: aws_config.DeploymentSettings, *, single_attempt: bool = False) -> aws_cli.AwsCli:
            executor = subject.admin.executor(aws_cli.execute_command if admin_spot_error else execute,
                                              single_attempt=single_attempt)
            return aws_cli.AwsCli(settings.profile, settings.region, settings.timeout_s, executor)

        _ = self.enterContext(mock.patch.object(deploy.Deployment, '_api', side_effect=admin_api))
        return subject, live_parameters

    def run_hana_setup_cli(self, subject: deploy.Deployment) -> tuple[int, str, str]:
        argv = ['deploy.py', 'hana-setup', '--approve-resources']
        output, errors = StringIO(), StringIO()
        child = deploy.Deployment(self.settings, self.state, subject.output / 'hana', clock=self.clock)
        with mock.patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(errors), \
                mock.patch.object(deploy, 'Deployment', side_effect=(subject, child)):
            status = deploy.main()
        return status, output.getvalue(), errors.getvalue()

    def test_scaler_policy_limits_instance_attribute_changes_to_hana_tags(self) -> None:
        template = ledger.mapping(cast(object, json.loads(deploy.HANA_TEMPLATE.read_text())))
        policy = ledger.mapping(ledger.mapping(ledger.mapping(template['Resources'])['ScalerPolicy'])['Properties'])
        statements = ledger.rows(ledger.mapping(policy['PolicyDocument']), 'Statement')
        action = 'ec2:ModifyInstanceAttribute'
        attribute_statements: list[dict[str, object]] = []
        for row in statements:
            actions = row.get('Action')
            if actions == action or (isinstance(actions, list) and action in cast(list[object], actions)):
                attribute_statements.append(row)
        statement, = attribute_statements
        self.assertEqual(statement['Action'], [action])
        self.assertEqual(statement['Condition'], {
            'StringEquals': {'ec2:ResourceTag/HanaDeployment': {'Ref': 'DeploymentId'},
                             'ec2:ResourceTag/Workload': 'hana'},
        })

    def test_hana_setup_updates_only_iam_policies_and_saves_the_template_hash(self) -> None:
        subject, parameters = self.policy_update_fixture([{
            'LogicalResourceId': 'ScalerPolicy', 'ResourceType': 'AWS::IAM::ManagedPolicy',
            'Action': 'Modify', 'Replacement': 'False',
        }])
        saved = remote.read_record(self.setup_path)
        evidence = self.book.read_snapshot()

        output = StringIO()
        with redirect_stdout(output):
            self.assertFalse(subject.hana_setup())
            self.assertEqual(self.calls, [])
            self.assertEqual(self.scaler_calls, [])
            self.assertEqual(remote.read_record(self.setup_path), saved)
            self.assertTrue(subject.hana_setup(approve_resources=True))

        lines = [line for line in output.getvalue().splitlines() if line.startswith('hana-setup probe:')]
        passed = [f'hana-setup probe: {action}: pass' for action in self.PROBE_OPERATIONS]
        self.assertEqual(lines, ['hana-setup probe: before the policy update', *passed,
                                 'hana-setup probe: after the policy update', *passed])
        self.assertEqual(self.scaler_calls,
                         [(phase, action) for phase in ('before', 'after') for action in self.PROBE_OPERATIONS])
        operations = [call[call.index('cloudformation') + 1] for call in self.calls]
        self.assertEqual(operations, ['describe-stacks', 'describe-stacks', 'create-change-set', 'wait',
                                     'describe-change-set', 'execute-change-set', 'wait'])
        for call in self.calls[2:]:
            self.assertEqual(call[call.index('--stack-name') + 1], 'fixture-stack-id')
        create = self.calls[operations.index('create-change-set')]
        self.assertEqual(create[create.index('--change-set-type') + 1], 'UPDATE')
        values = cast(object, json.loads(create[create.index('--parameters') + 1]))
        self.assertCountEqual(ledger.rows({'parameters': values}, 'parameters'),
                              [{'ParameterKey': key, 'UsePreviousValue': True} for key in parameters])
        self.assertEqual(remote.read_record(self.setup_path),
                         {**saved, 'template_sha256': sha256(deploy.HANA_TEMPLATE.read_bytes()).hexdigest()})
        self.assertEqual(self.book.read_snapshot(), evidence)

    def test_hana_setup_updates_a_version_one_stack_owned_box_then_runs_as_a_no_op(self) -> None:
        subject, _ = self.policy_update_fixture([{
            'LogicalResourceId': 'ScalerPolicy', 'ResourceType': 'AWS::IAM::ManagedPolicy',
            'Action': 'Modify', 'Replacement': 'False',
        }], stack_owned=True)
        saved = remote.read_record(self.setup_path)
        lifecycle = self.setup_path.with_name('lifecycle.json')
        registration = lifecycle.read_bytes()
        self.assertEqual(set(saved), {'bucket', 'data_volume', 'deployment', 'generation', 'instance',
            'parameters_sha256', 'region', 'requested', 'spot_request', 'stack', 'template_sha256', 'version'})
        self.assertEqual(saved['version'], 1)
        self.assertIs(saved['requested'], True)
        self.assertFalse((subject.output / 'hana').exists())

        with redirect_stdout(StringIO()) as output:
            self.assertTrue(subject.hana_setup(approve_resources=True))
        passed = [f'hana-setup probe: {action}: pass' for action in self.PROBE_OPERATIONS]
        self.assertEqual([line for line in output.getvalue().splitlines() if line.startswith('hana-setup probe:')],
                         ['hana-setup probe: before the policy update', *passed,
                          'hana-setup probe: after the policy update', *passed])
        checks = [('cloudformation', 'describe-stacks'), ('ec2', 'describe-instances'),
                  ('ec2', 'describe-volumes'), ('cloudformation', 'get-template')]
        probes = [('configure', 'export-credentials'), ('ec2', 'describe-instances'),
                  ('ec2', 'describe-spot-instance-requests'), ('ec2', 'describe-instance-attribute'),
                  ('ec2', 'modify-instance-attribute --dry-run'), ('ec2', 'start-instances --dry-run'),
                  ('ec2', 'stop-instances --dry-run'), ('s3api', 'put-object'), ('s3api', 'get-object')]
        self.assertEqual(self.policy_calls, [*checks, *probes, ('cloudformation', 'describe-stacks'),
            ('cloudformation', 'create-change-set'), ('cloudformation', 'wait change-set-create-complete'),
            ('cloudformation', 'describe-change-set'), ('cloudformation', 'execute-change-set'),
            ('cloudformation', 'wait stack-update-complete'), *probes])
        patched = self.stack_owned_hana_template()
        current = ledger.mapping(cast(object, json.loads(deploy.HANA_TEMPLATE.read_bytes())))
        ledger.mapping(patched['Resources'])['ScalerPolicy'] = ledger.mapping(current['Resources'])['ScalerPolicy']
        expected = {**saved, 'template_sha256': sha256(json.dumps(patched, sort_keys=True).encode()).hexdigest()}
        self.assertEqual(remote.read_record(self.setup_path), expected)
        self.assertEqual(lifecycle.read_bytes(), registration)

        self.policy_calls.clear()
        with redirect_stdout(StringIO()) as output:
            self.assertTrue(subject.hana_setup(approve_resources=True))
        self.assertIn('The Hana scaler policy is current.', output.getvalue())
        self.assertEqual([line for line in output.getvalue().splitlines() if line.startswith('hana-setup probe:')], passed)
        self.assertEqual(self.policy_calls, [*checks, *probes])
        self.assertEqual(remote.read_record(self.setup_path), expected)
        self.assertEqual(lifecycle.read_bytes(), registration)

    def test_hana_setup_probes_a_version_one_box_after_a_no_change_response(self) -> None:
        subject, _ = self.policy_update_fixture([], change_status='FAILED', stack_owned=True)
        with redirect_stdout(StringIO()) as output:
            self.assertTrue(subject.hana_setup(approve_resources=True))
        self.assertIn('The Hana scaler policy is current.', output.getvalue())
        self.assertEqual(self.scaler_calls, [('before', action) for _ in range(2) for action in self.PROBE_OPERATIONS])
        self.assertIn(('cloudformation', 'delete-change-set'), self.policy_calls)
        self.assertNotIn(('cloudformation', 'execute-change-set'), self.policy_calls)

    def test_hana_setup_refuses_other_old_records_with_resources(self) -> None:
        subject, _ = self.policy_update_fixture([], stack_owned=True)
        saved = remote.read_record(self.setup_path)
        cases: list[tuple[str, dict[str, object]]] = [
            (f'generation={value!r}', {**saved, 'generation': value})
            for value in (None, True, 1.0, '1', -1)
        ]
        cases.extend((f'extra {key}', {**saved, key: None}) for key in (
            'pending_launch', 'pending_volume', 'launch_template', 'security_group',
            'instance_type', 'zone', 'subnet', 'stop_pending', 'spot_max_price', 'unexpected',
        ))
        cases.extend((f'missing {key}', {name: value for name, value in saved.items() if name != key})
                     for key in ('generation', 'bucket', 'parameters_sha256'))
        cases.extend((
            ('wrong version', {**saved, 'version': 0}),
            ('wrong version type', {**saved, 'version': True}),
            ('wrong instance type', {**saved, 'instance': []}),
        ))
        for name, record in cases:
            with self.subTest(name=name):
                write_text_atomic(self.setup_path, json.dumps(record))
                with redirect_stdout(StringIO()), self.assertRaisesRegex(
                        deploy.DeploymentRefused, 'uses stack-owned resources'):
                    _ = subject.hana_setup(approve_resources=True)
                self.assertEqual(self.policy_calls, [])
                self.assertEqual(remote.read_record(self.setup_path), record)

    def test_hana_setup_refuses_a_stack_owned_box_with_a_different_scaler_user(self) -> None:
        subject, parameters = self.policy_update_fixture([], stack_owned=True)
        saved = self.setup_path.read_bytes()
        parameters['ScalerUser'] = 'fixture-other-scaler'
        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'scaler user differs'):
            _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(self.policy_calls, [('cloudformation', 'describe-stacks')])
        self.assertEqual(self.scaler_calls, [])
        self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_setup_refuses_to_change_a_stack_owned_disk_attachment(self) -> None:
        subject, _ = self.policy_update_fixture([{
            'LogicalResourceId': 'DataAttachment', 'ResourceType': 'AWS::EC2::VolumeAttachment',
            'Action': 'Modify', 'Replacement': 'False',
        }], stack_owned=True)
        saved = self.setup_path.read_bytes()
        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'only IAM policy modifications'):
            _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(self.policy_calls[-2:], [('cloudformation', 'describe-change-set'),
                                                ('cloudformation', 'delete-change-set')])
        self.assertNotIn(('cloudformation', 'execute-change-set'), self.policy_calls)
        self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_probe_reports_all_calls_after_a_failed_user_data_read(self) -> None:
        subject, _ = self.policy_update_fixture([{
            'LogicalResourceId': 'ScalerPolicy', 'ResourceType': 'AWS::IAM::ManagedPolicy',
            'Action': 'Modify', 'Replacement': 'False',
        }], scaler_faults={('before', 'describe-instance-attribute'): 'UnauthorizedOperation'})
        output = StringIO()
        with redirect_stdout(output):
            self.assertTrue(subject.hana_setup(approve_resources=True))

        lines = [line for line in output.getvalue().splitlines() if line.startswith('hana-setup probe:')]
        passed = [f'hana-setup probe: {action}: pass' for action in self.PROBE_OPERATIONS]
        before = [line if action != 'describe-instance-attribute' else
                  'hana-setup probe: describe-instance-attribute: fail (UnauthorizedOperation)'
                  for action, line in zip(self.PROBE_OPERATIONS, passed, strict=True)]
        self.assertEqual(lines, ['hana-setup probe: before the policy update', *before,
                                 'hana-setup probe: after the policy update', *passed])
        self.assertNotIn('private diagnostic', output.getvalue())

    def test_hana_setup_exits_one_when_the_last_probe_run_has_a_failure(self) -> None:
        for operation, code, bad_readback in (
            ('start-instances', 'UnauthorizedOperation', False),
            ('stop-instances', 'IncorrectInstanceState', False),
            ('start-instances', '', False),
            ('get-object', '', True),
        ):
            with self.subTest(operation=operation, code=code, bad_readback=bad_readback):
                self.scaler_calls.clear()
                subject, _ = self.policy_update_fixture([{
                    'LogicalResourceId': 'ScalerPolicy', 'ResourceType': 'AWS::IAM::ManagedPolicy',
                    'Action': 'Modify', 'Replacement': 'False',
                }], scaler_faults={('after', operation): code}, bad_readback=bad_readback)
                status, output, errors = self.run_hana_setup_cli(subject)
                self.assertEqual(status, 1)
                self.assertEqual(errors, '')
                after = output.split('hana-setup probe: after the policy update\n')[1]
                detail = f' ({code})' if code else ''
                self.assertEqual(after.splitlines(), [f'hana-setup probe: {action}: '
                    + (f'fail{detail}' if action == operation else 'pass') for action in self.PROBE_OPERATIONS])
                self.assertEqual(remote.read_record(self.setup_path)['template_sha256'],
                                 sha256(deploy.HANA_TEMPLATE.read_bytes()).hexdigest())

    def test_hana_setup_discards_non_policy_changes_and_replacements(self) -> None:
        changes: list[dict[str, object]] = []
        subject, _parameters = self.policy_update_fixture(changes)
        saved = self.setup_path.read_bytes()
        evidence = self.book.read_snapshot()
        kinds = {'Instance': 'AWS::EC2::Instance', 'DataVolume': 'AWS::EC2::Volume', 'ControlBucket': 'AWS::S3::Bucket'}
        unsafe = [(name, kind, 'Modify', replacement) for name, kind in kinds.items() for replacement in ('False', 'True')]
        unsafe.extend(('ScalerPolicy', 'AWS::IAM::ManagedPolicy', action, replacement) for action, replacement in (
            ('Add', 'False'), ('Remove', 'False'), ('Modify', 'True'), ('Modify', 'Conditional'), ('Modify', '')))

        for name, kind, action, replacement in unsafe:
            with self.subTest(name=name, action=action, replacement=replacement):
                change: dict[str, object] = {'LogicalResourceId': name, 'ResourceType': kind, 'Action': action}
                if replacement:
                    change['Replacement'] = replacement
                changes[:] = [
                    {'LogicalResourceId': 'FixturePolicy', 'ResourceType': 'AWS::IAM::ManagedPolicy',
                     'Action': 'Modify', 'Replacement': 'False'},
                    change,
                ]
                self.calls.clear()
                with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'IAM policy|replacement'):
                    _ = subject.hana_setup(approve_resources=True)
                operations = [call[call.index('cloudformation') + 1] for call in self.calls]
                self.assertNotIn('execute-change-set', operations)
                self.assertEqual(operations[-2:], ['describe-change-set', 'delete-change-set'])
                create = self.calls[operations.index('create-change-set')]
                delete = self.calls[-1]
                self.assertEqual(delete[delete.index('--change-set-name') + 1], create[create.index('--change-set-name') + 1])
                self.assertEqual(self.setup_path.read_bytes(), saved)
                self.assertEqual(self.book.read_snapshot(), evidence)

    def test_hana_setup_policy_update_refuses_changed_parameters(self) -> None:
        subject, parameters = self.policy_update_fixture([])
        saved = self.setup_path.read_bytes()
        text_setting(self.workload, 'spot_max_usd_per_hour', str(Decimal(parameters['SpotMaxPrice']) / 2))
        _ = subject.prepare_hana()

        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'parameters or region changed'):
            _ = subject.hana_setup(approve_resources=True)

        self.assertEqual([call[call.index('cloudformation') + 1] for call in self.calls], ['describe-stacks'])
        self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_policy_update_refuses_changed_live_parameters(self) -> None:
        subject, live_parameters = self.policy_update_fixture([])
        saved = self.setup_path.read_bytes()
        live_parameters['ScalerUser'] = 'fixture-other-scaler'

        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'live Hana stack parameters differ'):
            _ = subject.hana_setup(approve_resources=True)

        self.assertEqual([call[call.index('cloudformation') + 1] for call in self.calls], ['describe-stacks', 'describe-stacks'])
        self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_policy_update_discards_changed_effective_parameters(self) -> None:
        subject, _ = self.policy_update_fixture([{
            'LogicalResourceId': 'ScalerPolicy', 'ResourceType': 'AWS::IAM::ManagedPolicy',
            'Action': 'Modify', 'Replacement': 'False',
        }], changed_scaler_user='fixture-new-scaler')
        saved = self.setup_path.read_bytes()

        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'change set parameters differ'):
            _ = subject.hana_setup(approve_resources=True)

        operations = [call[call.index('cloudformation') + 1] for call in self.calls]
        self.assertEqual(operations[-2:], ['describe-change-set', 'delete-change-set'])
        self.assertNotIn('execute-change-set', operations)
        self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_policy_update_refuses_when_the_lifecycle_stays_busy(self) -> None:
        subject, _ = self.policy_update_fixture([])
        subject.pause = self.advance_pause
        path = self.setup_path.with_name('lifecycle.json')
        lifecycle = remote.read_record(path)
        busy_values: tuple[dict[str, object], ...] = (
            {'state': 'ready'}, {'claims': [{'execution': 'fixture-claim'}]},
            {'start': {'token': 'fixture-start'}}, {'operation': {'action': 'start'}},
        )
        for values in busy_values:
            with self.subTest(values=values):
                self.calls.clear()
                write_text_atomic(path, json.dumps({**lifecycle, **values}))
                state = values.get('state', 'idle-stopped')
                holder = 'unrecorded' if 'claims' in values else 'none'
                with redirect_stdout(StringIO()) as output, mock.patch.object(time, 'monotonic', self.clock), \
                        self.assertRaisesRegex(deploy.DeploymentRefused,
                            rf'still busy after 45 s \(state {state}, claim holder {holder}\).*again when the box is idle'):
                    _ = subject.hana_setup(approve_resources=True)
                self.assertIn('waiting up to 45 s for Hana to become idle', output.getvalue())
                self.assertEqual(self.instance_reads, [])
                self.assertEqual(self.scaler_calls, [])
                self.assertEqual(self.policy_calls, [])

    def test_hana_setup_takes_the_box_at_the_deadline_and_holds_it_through_the_probe(self) -> None:
        subject, _ = self.policy_update_fixture([{
            'LogicalResourceId': 'ScalerPolicy', 'ResourceType': 'AWS::IAM::ManagedPolicy',
            'Action': 'Modify', 'Replacement': 'False',
        }], stack_owned=True, current_guest_image=True)
        controller = hana_box.HanaController(self.state, self.settings, self.workload, hana_box.ProviderNotInstalled(),
                                            clock=self.clock)
        path = self.setup_path.with_name('lifecycle.json')
        idle = path.read_bytes()
        self.assertIsInstance(controller.request('fixture-live-claim'), hana_box.ClaimCommitted)
        lifecycle = remote.read_record(path)
        write_text_atomic(path, json.dumps(lifecycle | {'state': 'starting'}))
        live_claim, = ledger.rows(lifecycle, 'claims')
        pauses: list[float] = []

        def advance(seconds: float) -> None:
            self.assertEqual(self.policy_calls, [])
            self.assertIsInstance(controller.inspect(), hana_box.HanaLifecycleEvidence)
            pauses.append(seconds)
            self.advance_pause(seconds)
            if len(pauses) == 1:
                write_text_atomic(path, json.dumps(lifecycle | {
                    'state': 'ready', 'claims': [live_claim | {'active': True}],
                }))
            elif len(pauses) == 3:
                _ = path.write_bytes(idle)

        original_probe = subject._probe_hana_scaler  # pyright: ignore[reportPrivateUsage]

        subject.pause = advance
        with ThreadPoolExecutor(max_workers=1) as worker:
            def check_claim(provider: AwsHanaProvider, identity: hana_box.DeploymentIdentity) -> bool:
                claim = worker.submit(controller.request, 'fixture-late-claim')
                self.assertIsInstance(claim.result(timeout=5), hana_box.HanaResourcesHeld)
                self.assertIsInstance(worker.submit(controller.tick).result(timeout=5), hana_box.HanaResourcesHeld)
                self.assertEqual(path.read_bytes(), idle)
                return original_probe(provider, identity)

            with mock.patch.object(time, 'monotonic', self.clock), \
                    mock.patch.object(subject, '_probe_hana_scaler', side_effect=check_claim) as probe:
                status, output, errors = self.run_hana_setup_cli(subject)
        self.assertEqual((status, errors), (0, ''), output)
        self.assertEqual(pauses, [15, 15, 15])
        self.assertEqual([line for line in output.splitlines() if line.startswith('hana-setup:')][:4], [
            'hana-setup: waiting up to 45 s for Hana to become idle (state starting, claim holder local process).',
            'hana-setup: still waiting for Hana (state ready, claim holder local process, 15 s of 45 s).',
            'hana-setup: still waiting for Hana (state ready, claim holder local process, 30 s of 45 s).',
            'hana-setup: Hana is idle-stopped. Setup holds the box.',
        ])
        self.assertEqual(probe.call_count, 2)
        self.assertIn(('cloudformation', 'execute-change-set'), self.policy_calls)
        self.assertIsInstance(controller.request('fixture-late-claim'), hana_box.ClaimCommitted)

    def test_exclusive_commands_refuse_while_approved_hana_setup_holds_the_box(self) -> None:
        subject, _ = self.policy_update_fixture([], stack_owned=True)
        unexpected_aws = mock.Mock(side_effect=AssertionError('Unexpected AWS call'))
        plan = plan_run.PlanRun(unexpected_aws, unexpected_aws, unexpected_aws)
        planned = deploy.Deployment(self.settings, self.state, self.root / 'plan', mode=plan)
        image = hana_image.BuiltImage('fixture-image', self.root, self.root / 'image.raw', 'raw', 'uefi', 1024)
        operations: tuple[tuple[str, Callable[[], object]], ...] = (
            ('hana-down', lambda: subject.hana_down(approve_destroy=True)),
            ('hana-image', lambda: subject.hana_image(self.root, approve_resources=True)),
            ('hana-setup', lambda: subject.hana_setup(approve_resources=True)),
            ('hana-setup --plan', planned.hana_setup),
            ('prepare --workload hana', subject.prepare_hana),
            ('prepare', subject.prepare),
            ('apply', lambda: subject.apply(approve_resources=True)),
            ('down', lambda: subject.down(approve_destroy=True)),
        )

        def check_competing_commands(*_arguments: object) -> bool:
            with (mock.patch.object(subject, '_api', side_effect=AssertionError('Unexpected AWS call')) as api,
                    mock.patch.object(subject, 'pause', side_effect=AssertionError('Unexpected idle wait')),
                    mock.patch.object(hana_image, 'read_built', return_value=image)):
                for name, operation in operations:
                    with self.subTest(command=name), self.assertRaisesRegex(deploy.HanaControlRefused,
                            f'^{name}: Another command is using the Hana resources. '
                            + r'Run the same command again when that command ends\.$'):
                        _ = operation()
                api.assert_not_called()
                unexpected_aws.assert_not_called()
            return True

        with (redirect_stdout(StringIO()),
                mock.patch.object(subject, '_update_stack_owned_hana', side_effect=check_competing_commands) as update):
            self.assertTrue(subject.hana_setup(approve_resources=True))
        update.assert_called_once()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.policy_calls, [])
        self.assertEqual(self.scaler_calls, [])

    def test_hana_setup_rejects_a_wait_that_cannot_cover_the_warm_idle_and_stop_intervals(self) -> None:
        subject, _ = self.policy_update_fixture([])
        for timeout in ('34', '35'):
            with self.subTest(timeout=timeout), redirect_stdout(StringIO()):
                numeric_settings(self.workload, {'setup_idle_wait_timeout_s': timeout})
                with self.assertRaisesRegex(deploy.HanaSetupTimingRefused,
                        'setup_idle_wait_timeout_s must exceed idle_stop_seconds plus stop_wait_timeout_s'):
                    _ = subject.hana_setup(approve_resources=True)
                self.assertEqual(self.calls, [])
        numeric_settings(self.workload, {'setup_idle_wait_timeout_s': '36'})
        self.assertEqual(deploy.HanaSetupTiming.read(conf.read(self.workload)).idle_wait_s, 36)

    def test_hana_setup_interrupt_while_waiting_leaves_the_lifecycle_and_aws_untouched(self) -> None:
        subject, _ = self.policy_update_fixture([], stack_owned=True)
        path = self.setup_path.with_name('lifecycle.json')
        write_text_atomic(path, json.dumps(remote.read_record(path) | {'state': 'ready'}))
        busy = path.read_bytes()

        def interrupt(_seconds: float) -> None:
            raise KeyboardInterrupt

        subject.pause = interrupt
        status, output, errors = self.run_hana_setup_cli(subject)
        self.assertEqual(status, 130)
        self.assertIn('waiting up to 45 s for Hana to become idle', output)
        self.assertEqual(errors, 'hana-setup: interrupted\n')
        self.assertEqual(self.policy_calls, [])
        self.assertEqual(path.read_bytes(), busy)
        with file_lock.exclusive(self.state / 'controller.lock', wait=False) as result:
            self.assertIs(result, file_lock.LockResult.ACQUIRED)

    def test_hana_policy_update_requires_the_live_instance_to_be_stopped(self) -> None:
        subject, _ = self.policy_update_fixture([], live_state='running')

        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'stopped.*again when the box is idle'):
            _ = subject.hana_setup(approve_resources=True)

        self.assertEqual(len(self.instance_reads), 1)
        self.assertEqual(self.scaler_calls, [])
        self.assertEqual([call[call.index('cloudformation') + 1] for call in self.calls], ['describe-stacks'])

    def test_hana_policy_update_requires_saved_identities_and_lifecycle_registration(self) -> None:
        subject, _parameters = self.policy_update_fixture([])
        saved = remote.read_record(self.setup_path)

        for key in ('instance', 'data_volume', 'bucket'):
            with self.subTest(key=key):
                write_text_atomic(self.setup_path, json.dumps({name: value for name, value in saved.items() if name != key}))
                self.calls.clear()
                with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, f'lacks {key}'):
                    _ = subject.hana_setup(approve_resources=True)
                self.assertEqual([call[call.index('cloudformation') + 1] for call in self.calls], ['describe-stacks'])

        write_text_atomic(self.setup_path, json.dumps(saved))
        self.setup_path.with_name('lifecycle.json').unlink()
        self.calls.clear()
        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'no lifecycle registration'):
            _ = subject.hana_setup(approve_resources=True)
        self.assertEqual([call[call.index('cloudformation') + 1] for call in self.calls], ['describe-stacks'])
        self.assertEqual(remote.read_record(self.setup_path), saved)

    def test_hana_policy_update_refuses_changed_resource_identities(self) -> None:
        subject, _parameters = self.policy_update_fixture([])
        saved = remote.read_record(self.setup_path)
        saved['bucket'] = 'fixture-other-bucket'
        write_text_atomic(self.setup_path, json.dumps(saved))

        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'resource identities changed'):
            _ = subject.hana_setup(approve_resources=True)

        self.assertEqual([call[call.index('cloudformation') + 1] for call in self.calls], ['describe-stacks'])
        self.assertEqual(remote.read_record(self.setup_path), saved)

    def test_hana_policy_update_refuses_a_changed_disk_attachment(self) -> None:
        subject, _parameters = self.policy_update_fixture([], volume_device='/dev/sdg')
        saved = self.setup_path.read_bytes()

        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'retained Hana data attachment'):
            _ = subject.hana_setup(approve_resources=True)

        self.assertEqual(self.scaler_calls, [])
        self.assertEqual([call[call.index('cloudformation') + 1] for call in self.calls], ['describe-stacks'])
        self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_policy_update_refuses_stale_preparation(self) -> None:
        subject, _parameters = self.policy_update_fixture([])
        saved = self.setup_path.read_bytes()
        self.clock.advance(301)

        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'preparation expired'):
            _ = subject.hana_setup(approve_resources=True)

        self.assertEqual(self.calls, [])
        self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_policy_update_refuses_changed_region(self) -> None:
        subject, _parameters = self.policy_update_fixture([])
        saved = self.setup_path.read_bytes()
        text_setting(self.workload, 'region', 'us-east-1')
        text_setting(self.workload, 'availability_zone', 'us-east-1b')
        _ = subject.prepare_hana()

        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'parameters or region changed'):
            _ = subject.hana_setup(approve_resources=True)

        self.assertEqual([call[call.index('cloudformation') + 1] for call in self.calls], ['describe-stacks'])
        self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_policy_update_refuses_a_stack_that_disappears(self) -> None:
        subject, _parameters = self.policy_update_fixture([], stack_disappears=True)
        saved = self.setup_path.read_bytes()

        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'stack is absent'):
            _ = subject.hana_setup(approve_resources=True)

        self.assertEqual([call[call.index('cloudformation') + 1] for call in self.calls], ['describe-stacks', 'describe-stacks'])
        self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_policy_update_discards_a_paginated_change_set(self) -> None:
        subject, _parameters = self.policy_update_fixture([{
            'LogicalResourceId': 'ScalerPolicy', 'ResourceType': 'AWS::IAM::ManagedPolicy',
            'Action': 'Modify', 'Replacement': 'False',
        }], next_token='fixture-next-page')
        saved = self.setup_path.read_bytes()

        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'more than one page'):
            _ = subject.hana_setup(approve_resources=True)

        operations = [call[call.index('cloudformation') + 1] for call in self.calls]
        self.assertIn('--no-paginate', self.calls[operations.index('describe-change-set')])
        self.assertEqual(operations[-2:], ['describe-change-set', 'delete-change-set'])
        self.assertNotIn('execute-change-set', operations)
        create, delete = self.calls[operations.index('create-change-set')], self.calls[-1]
        self.assertEqual(delete[delete.index('--change-set-name') + 1], create[create.index('--change-set-name') + 1])
        self.assertEqual(delete[delete.index('--stack-name') + 1], 'fixture-stack-id')
        self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_policy_update_discards_empty_and_failed_change_sets(self) -> None:
        for status in ('CREATE_COMPLETE', 'FAILED'):
            with self.subTest(status=status):
                subject, _parameters = self.policy_update_fixture([], change_status=status)
                saved = self.setup_path.read_bytes()
                self.calls.clear()

                with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'no changes|failed to create'):
                    _ = subject.hana_setup(approve_resources=True)

                operations = [call[call.index('cloudformation') + 1] for call in self.calls]
                self.assertEqual(operations[-2:], ['describe-change-set', 'delete-change-set'])
                self.assertNotIn('execute-change-set', operations)
                self.assertFalse(any('stack-update-complete' in call for call in self.calls))
                self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_policy_update_discards_a_definitely_refused_execution(self) -> None:
        subject, _parameters = self.policy_update_fixture([{
            'LogicalResourceId': 'ScalerPolicy', 'ResourceType': 'AWS::IAM::ManagedPolicy',
            'Action': 'Modify', 'Replacement': 'False',
        }], execution='refused')
        saved = self.setup_path.read_bytes()

        with redirect_stdout(StringIO()), self.assertRaisesRegex(aws_cli.AwsUnavailable, 'AccessDenied'):
            _ = subject.hana_setup(approve_resources=True)

        operations = [call[call.index('cloudformation') + 1] for call in self.calls]
        self.assertEqual(operations[-2:], ['execute-change-set', 'delete-change-set'])
        execute, delete = self.calls[-2:]
        self.assertEqual(delete[delete.index('--change-set-name') + 1], execute[execute.index('--change-set-name') + 1])
        self.assertEqual(delete[delete.index('--stack-name') + 1], 'fixture-stack-id')
        self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_policy_update_keeps_a_change_set_with_an_uncertain_execution(self) -> None:
        for execution in ('timeout', 'unknown'):
            with self.subTest(execution=execution):
                subject, _parameters = self.policy_update_fixture([{
                    'LogicalResourceId': 'ScalerPolicy', 'ResourceType': 'AWS::IAM::ManagedPolicy',
                    'Action': 'Modify', 'Replacement': 'False',
                }], execution=execution)
                saved = self.setup_path.read_bytes()
                self.calls.clear()

                with redirect_stdout(StringIO()), self.assertRaises(aws_cli.AwsUnavailable):
                    _ = subject.hana_setup(approve_resources=True)

                operations = [call[call.index('cloudformation') + 1] for call in self.calls]
                self.assertEqual(operations[-1], 'execute-change-set')
                self.assertNotIn('delete-change-set', operations)
                self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_policy_update_keeps_the_change_set_without_a_definite_first_attempt_refusal(self) -> None:
        for error in (
            'An error occurred (InvalidChangeSetStatus) when calling the ExecuteChangeSet operation (reached max retries: 2)',
            'An error occurred (AccessDenied) when calling the DescribeChangeSet operation',
            'An error occurred (InternalFailure) when calling the ExecuteChangeSet operation',
        ):
            with self.subTest(error=error):
                self.calls.clear()
                subject, _ = self.policy_update_fixture([{
                    'LogicalResourceId': 'ScalerPolicy', 'ResourceType': 'AWS::IAM::ManagedPolicy',
                    'Action': 'Modify', 'Replacement': 'False',
                }], execution_error=error)
                saved = self.setup_path.read_bytes()
                with redirect_stdout(StringIO()), self.assertRaises(aws_cli.AwsUnavailable):
                    _ = subject.hana_setup(approve_resources=True)
                operations = [call[call.index('cloudformation') + 1] for call in self.calls]
                self.assertEqual(operations[-1], 'execute-change-set')
                self.assertNotIn('delete-change-set', operations)
                self.assertEqual(self.setup_path.read_bytes(), saved)

    def test_hana_setup_policy_update_failure_prints_the_update_error_and_keeps_the_old_hash(self) -> None:
        subject, _parameters = self.policy_update_fixture([{
            'LogicalResourceId': 'ScalerPolicy', 'ResourceType': 'AWS::IAM::ManagedPolicy',
            'Action': 'Modify', 'Replacement': 'False',
        }], fail_update=True)
        saved = self.setup_path.read_bytes()
        evidence = self.book.read_snapshot()

        status, output, errors = self.run_hana_setup_cli(subject)

        self.assertEqual(status, 1)
        self.assertIn('hana-setup: updating the Hana IAM policy failed after', output)
        self.assertEqual(errors, 'aws_deploy: fixture update failed\n')
        self.assertEqual(self.scaler_calls, [('before', action) for action in self.PROBE_OPERATIONS])
        self.assertEqual(self.calls[-1][self.calls[-1].index('cloudformation') + 1:][:2], ['wait', 'stack-update-complete'])
        self.assertFalse(any('describe-stack-events' in call or 'delete-stack' in call for call in self.calls))
        self.assertEqual(self.setup_path.read_bytes(), saved)
        self.assertEqual(self.book.read_snapshot(), evidence)

    def test_hana_setup_spot_lookup_reports_the_code_with_a_json_error_format_override(self) -> None:
        subject, _ = self.policy_update_fixture([], admin_spot_error=
            'An error occurred (InvalidSpotInstanceRequestID.NotFound) when calling the DescribeSpotInstanceRequests operation: '
            + 'The request sir-0123456789abcdef0 was not found')
        saved = remote.read_record(self.setup_path)
        saved['template_sha256'] = sha256(deploy.HANA_TEMPLATE.read_bytes()).hexdigest()
        write_text_atomic(self.setup_path, json.dumps(saved))

        status, output, errors = self.run_hana_setup_cli(subject)

        self.assertEqual(status, 1)
        self.assertEqual(errors, 'aws_deploy: describe-spot-instance-requests: fail (InvalidSpotInstanceRequestID.NotFound)\n')
        self.assertNotIn('sir-0123456789abcdef0', output)
        self.assertEqual(self.scaler_calls, [])

    def test_failed_hana_create_reports_resource_failure_and_retries_with_changed_ceiling(self) -> None:
        fake = FakeEc2(launch_on_execute=True, fail_wait='stack-create-complete')
        subject = self.subject(fake)
        _ = subject.prepare_hana()

        with redirect_stdout(StringIO()), self.assertRaises(aws_cli.AwsUnavailable) as failure:
            _ = subject.hana_setup(approve_resources=True)
        self.assertIn('Instance (AWS::EC2::Instance): CREATE_FAILED: No capacity in this zone', str(failure.exception))
        self.assertLess(fake.operations.index('describe-stack-events'), fake.operations.index('delete-stack'))
        self.assertFalse(self.setup_path.exists())
        state = self.book.read_snapshot()
        self.assertIsInstance(state.hana_compute[0].state, ledger.HanaExposureClosed)
        self.assertIsInstance(state.hana_storage[0].state, ledger.HanaExposureClosed)

        ceiling = str(state.hana_compute[0].usd_per_hour / 2)
        text_setting(self.workload, 'spot_max_usd_per_hour', ceiling)
        _ = subject.prepare_hana()
        with redirect_stdout(StringIO()), self.assertRaisesRegex(aws_cli.AwsUnavailable, 'fixture rollback'):
            _ = subject.hana_setup(approve_resources=True)
        first, second = [call for call in self.calls if 'create-change-set' in call]
        self.assertIn('CREATE', first)
        self.assertIn('CREATE', second)
        parameters = cast(object, json.loads(second[second.index('--parameters') + 1]))
        self.assertIn({'ParameterKey': 'SpotMaxPrice', 'ParameterValue': ceiling},
                      ledger.rows({'parameters': parameters}, 'parameters'))

    def test_failed_hana_cleanup_keeps_the_setup_and_reservation(self) -> None:
        fake = FakeEc2(launch_on_execute=True, fail_wait='stack-create-complete', fail='delete-volume')
        subject = self.subject(fake)
        _ = subject.prepare_hana()

        with redirect_stdout(StringIO()), self.assertRaisesRegex(aws_cli.AwsUnavailable, 'did not remove what it made'):
            _ = subject.hana_setup(approve_resources=True)
        saved = remote.read_record(self.setup_path)
        exposure, = self.book.read_snapshot().hana_compute
        self.assertEqual(saved['generation'], exposure.generation)
        self.assertIs(saved['requested'], True)
        self.assertIsInstance(exposure.state, ledger.HanaExposureOpen)
        self.assertFalse(fake.stack)
        self.assertTrue(fake.disks)
        fake.fail = ''
        with redirect_stdout(StringIO()):
            self.assertTrue(subject.hana_down(approve_destroy=True))
        self.assertEqual(fake.leftovers(), ([], [], [], [], False))
        self.assertFalse(self.setup_path.exists())
        state = self.book.read_snapshot()
        storage, = state.hana_storage
        self.assertEqual(storage.state, ledger.HanaExposureClosed(self.clock.now))
        charges = ledger.hana_intervals(state, self.clock.now, 0)
        self.assertEqual(ledger.hana_intervals(state, self.clock.now + 3600, 0), charges)

    def test_hana_create_timeout_before_rollback_keeps_the_setup_and_reservation(self) -> None:
        fake = FakeEc2(launch_on_execute=True)
        subject = self.subject(fake, waiter_timeout=True)
        _ = subject.prepare_hana()

        with redirect_stdout(StringIO()), self.assertRaisesRegex(aws_cli.AwsUnavailable, 'terminal rollback') as failure:
            _ = subject.hana_setup(approve_resources=True)
        self.assertNotIsInstance(failure.exception, deploy.HanaCreateRolledBack)
        self.assertTrue(self.setup_path.exists())
        state = self.book.read_snapshot()
        self.assertIsInstance(state.hana_compute[0].state, ledger.HanaExposureOpen)
        self.assertIsInstance(state.hana_storage[0].state, ledger.HanaExposureOpen)
        self.assertTrue(fake.stack)
        self.assertNotIn('delete-stack', fake.operations)

    def test_failed_hana_create_deletes_resources_retained_by_stack_delete(self) -> None:
        fake = FakeEc2(fail_wait='stack-create-complete')
        subject = self.subject(fake, retain_on_delete=True)
        _ = subject.prepare_hana()

        with redirect_stdout(StringIO()), self.assertRaises(deploy.HanaCreateRolledBack):
            _ = subject.hana_setup(approve_resources=True)

        waited = fake.operations.index('wait stack-delete-complete')
        self.assertGreater(fake.operations.index('delete-volume'), waited)
        self.assertGreater(fake.operations.index('delete-bucket'), waited)
        self.assertEqual(fake.leftovers(), ([], [], [], [], False))
        self.assertFalse(self.setup_path.exists())
        state = self.book.read_snapshot()
        self.assertIsInstance(state.hana_compute[0].state, ledger.HanaExposureClosed)
        self.assertIsInstance(state.hana_storage[0].state, ledger.HanaExposureClosed)

    def test_hana_setup_clears_a_missing_stack_record_before_creating_again(self) -> None:
        fake = FakeEc2(launch_on_execute=True, fail_wait='stack-create-complete')
        _ = fake.launch(tagged=True)
        created = self.clock.now
        fake.create_retained()
        fake.roll_back()
        self.deleted_stacks['previous-stack'] = (created, fake.events.copy())
        fake.events.clear()
        subject = self.subject(fake)
        previous_generation = self.write_leftover(subject)

        output = StringIO()
        with redirect_stdout(output), self.assertRaisesRegex(aws_cli.AwsUnavailable, 'fixture rollback'):
            _ = subject.hana_setup(approve_resources=True)
        self.assertIn('hana-setup: cleared the saved setup for the missing stack', output.getvalue())
        create = fake.operations.index('create-change-set')
        self.assertLess(fake.operations.index('wait instance-terminated'), create)
        self.assertLess(fake.operations.index('delete-volume'), create)
        self.assertLess(fake.operations.index('delete-bucket'), create)
        price_read = next(index for index, call in enumerate(self.calls) if 'describe-spot-price-history' in call)
        bucket_delete = next(index for index, call in enumerate(self.calls) if 'delete-bucket' in call)
        create_call = next(index for index, call in enumerate(self.calls) if 'create-change-set' in call)
        self.assertLess(bucket_delete, price_read)
        self.assertLess(price_read, create_call)
        self.assertEqual(fake.leftovers(), ([], [], [], [], False))
        evidence = self.book.read_snapshot()
        first, second = evidence.hana_compute
        self.assertEqual(first.generation, previous_generation)
        self.assertIsInstance(first.state, ledger.HanaExposureClosed)
        self.assertGreater(second.generation, previous_generation)
        self.assertIsInstance(evidence.hana_storage[0].state, ledger.HanaExposureClosed)

    def test_hana_setup_cleans_up_a_missing_requested_stack_before_image_validation(self) -> None:
        fake = FakeEc2()
        fake.create_retained()
        fake.roll_back()
        self.deleted_stacks['previous-stack'] = (self.clock.now, fake.events.copy())
        subject = self.subject(fake)
        _ = self.write_leftover(subject)

        with mock.patch.object(subject, '_validate_inputs', side_effect=aws_cli.AwsUnavailable('Image is unavailable')), \
                redirect_stdout(StringIO()), self.assertRaisesRegex(aws_cli.AwsUnavailable, 'Image is unavailable'):
            _ = subject.hana_setup(approve_resources=True)

        self.assertEqual(fake.operations[:3], ['describe-stacks', 'list-stacks', 'describe-stack-events'])
        self.assertEqual(fake.leftovers(), ([], [], [], [], False))
        self.assertFalse(self.setup_path.exists())
        state = self.book.read_snapshot()
        self.assertIsInstance(state.hana_compute[0].state, ledger.HanaExposureClosed)
        self.assertIsInstance(state.hana_storage[0].state, ledger.HanaExposureClosed)
        self.assertFalse(any('describe-spot-price-history' in call for call in self.calls))

    def test_hana_setup_refuses_to_clear_a_record_without_deleted_stack_evidence(self) -> None:
        fake = FakeEc2()
        self.deleted_stacks['older-stack'] = (self.clock.now - 1, [])
        subject = self.subject(fake)
        _ = self.write_leftover(subject)
        saved = self.setup_path.read_bytes()
        evidence = self.book.read_snapshot()

        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'down --workload hana --approve-destroy'):
            _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(fake.operations, ['describe-stacks', 'list-stacks'])
        self.assertEqual(self.setup_path.read_bytes(), saved)
        self.assertEqual(self.book.read_snapshot(), evidence)

    def test_hana_down_clears_the_record_and_charges_without_deleted_stack_history(self) -> None:
        fake = FakeEc2()
        subject = self.subject(fake)
        generation = self.write_leftover(subject)
        self.assertIs(remote.read_record(self.setup_path)['requested'], True)
        before = self.book.read_snapshot()
        self.assertIsInstance(before.hana_compute[0].state, ledger.HanaExposureOpen)
        self.assertIsInstance(before.hana_storage[0].state, ledger.HanaExposureOpen)

        output = StringIO()
        with redirect_stdout(output):
            self.assertTrue(subject.hana_down(approve_destroy=True))

        self.assertIn('down --workload hana: no deleted Hana stack since setup started', output.getvalue())
        self.assertFalse(self.setup_path.exists())
        self.assertEqual(fake.leftovers(), ([], [], [], [], False))
        after = self.book.read_snapshot()
        compute, = after.hana_compute
        storage, = after.hana_storage
        self.assertEqual(compute.generation, generation)
        closed = ledger.HanaExposureClosed(self.clock.now)
        self.assertEqual(compute.state, closed)
        self.assertEqual(storage.state, closed)

    def test_hana_setup_refuses_a_missing_stack_record_that_names_an_instance(self) -> None:
        fake = FakeEc2()
        instance = fake.launch(tagged=True)
        subject = self.subject(fake)
        _ = self.write_leftover(subject, instance=instance)
        workload = conf.read(self.workload)
        _ = hana_image.save(self.state, hana_image.RegisteredImage('fixture', workload.text('ami_id'),
            workload.text('ami_owner_id'), workload.text('vpc_id'), workload.text('subnet_id'),
            hana_render.guest_digest(self.workload)))
        saved = self.setup_path.read_bytes()
        evidence = self.book.read_snapshot()

        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.DeploymentRefused, 'down --workload hana --approve-destroy'):
            _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(fake.operations, [])
        self.assertEqual(self.setup_path.read_bytes(), saved)
        self.assertEqual(self.book.read_snapshot(), evidence)


class DeploymentTests(CostFixture):
    now: int = 0
    bundle: Path = Path()
    credentials: Path = Path()
    notification_file: Path = Path()
    price_file: Path = Path()
    patch_sections: ClassVar[dict[str, str]] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.object(box, 'urlopen', side_effect=AssertionError('tests require a fake tailnet transport'))))
        self.now = int(datetime.fromisoformat(DAY + "T23:59:00+00:00").timestamp())
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        bootstrap = self.bundle / 'bootstrap.sh'
        _ = bootstrap.write_text('#!/usr/bin/env bash\ntrue\n')
        hosts = self.root / 'nixos/hosts'
        hosts.mkdir(parents=True)
        modules = hosts.parent / 'modules/linux'
        modules.mkdir(parents=True)
        _ = (modules / 'tailscale.nix').write_text(
            '# The module enables Tailscale SSH.\n{ services.tailscale = {\n' +
            '  enable = true;\n  extraSetFlags = [ "--ssh" ];\n  openFirewall = true;\n}; }\n')
        _ = (hosts / 'aws-build-base.nix').write_text(
            '# The base shares the transport module.\n{ ... }: { imports = [ ../modules/linux/tailscale.nix ]; }')
        self.notification_file = self.root / 'notification-email'
        _ = self.notification_file.write_text('build@example.invalid\n')
        self.text_setting('notification_email_file', str(self.notification_file))
        self.price_file = self.root / 'deployment-price'
        values = {
            'admin_secret_file': str(self.root / 'admin.gpg'), 'deployment_profile': 'build-admin', 'stack_name': 'fixture-build',
            'ami_id': 'ami-fixture', 'ami_owner_id': '123456789012', 'nixos_release': '26.05',
            'availability_zone': 'us-east-1a', 'vpc_id': 'vpc-fixture', 'subnet_id': 'subnet-fixture',
            'root_device_name': '/dev/xvda', 'scaler_user_name': 'build-scaler',
        }
        values.update({
            'spot_max_usd_per_hour': '0.5',
            'budget_notice_low_percent': '50', 'budget_notice_high_percent': '80', 'budget_stop_percent': '100',
            'deployment_timeout_s': '30', 'preflight_max_age_s': '300', 'fixed_usd_per_month': '0.001',
            'retained_storage_usd_per_month': '0', 'root_storage_usd_per_month': '0',
            'ipv4_usd_per_month': '0', 'gp3_iops_usd_per_month': '0', 'gp3_throughput_usd_per_month': '0',
            'region': 'us-east-1', 'cost_explorer_lookback_days': '1',
            'cost_explorer_tag_key': 'Project', 'cost_explorer_tag_value': 'fixture-build', 'accounting_zone': 'UTC',
        })
        for key, value in values.items():
            self.replace(key, value)
        gates = self.root / 'cloud_gate.conf'
        self.configure_gate(
            "regions=us-east-1\nroot_gib=40\nspot_max_usd_per_hour=0.8\nshared_usd_per_month=1\n")
        # Quota cases assume these targets; pin them in place so a /tune of the shipped values never breaks a test.
        _ = gates.write_text(re.sub(r'^(gpu_spot_quota_request_vcpus)=.*$', r'\1=4',
                                    re.sub(r'^(spot_quota_request_vcpus)=.*$', r'\1=128', gates.read_text(), flags=re.M), flags=re.M))
        _ = cast(object, self.enterContext(mock.patch.object(admin_secrets.AdminSecretSession, 'get', return_value=
            admin_secrets.AdminCredentials('fixture-admin-id', 'fixture-admin-secret', 'fixture-client', 'fixture-oauth-secret'))))
        self.credentials = self.root / 'credentials'
        _ = self.credentials.write_text('')
        self.credentials.chmod(0o600)
        bin_path = self.root / 'bin'
        bin_path.mkdir()
        self.fake_commands(f'#!{sys.executable}\n' + FAKE_AWS, 'aws')
        status = {'Self': {'HostName': 'natedev', 'DNSName': 'natedev.tail21e383.ts.net.', 'TailscaleIPs': ['100.64.0.1']},
                  'Peer': {'mac': {'HostName': "Nate's MacBook Pro 2023", 'DNSName': 'nates-macbook-pro-2023.tail21e383.ts.net.', 'TailscaleIPs': ['100.64.0.2']}}}
        self.fake_commands(f'#!{sys.executable}\nprint({json.dumps(status)!r})\n', 'tailscale')
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            'PATH': f'{bin_path}{os.pathsep}{os.environ["PATH"]}', 'DEPLOY_FIXTURE': str(self.root),
            'AWS_SHARED_CREDENTIALS_FILE': str(self.credentials),
            'BOX_REAL_GIT': shutil.which('git') or 'git',
            'SCRATCH_ROOT': str(self.root / 'scratch'), 'SCRATCH_HOLDS': str(self.root / 'holds.json'),
        })))
        _ = cast(object, self.enterContext(mock.patch.object(target_config, 'ssh_target_state_directory', return_value=self.state)))
        self.set_clock(self.now)
        if self._testMethodName not in self.process_tests:
            process = SimpleNamespace(**{**cast(Mapping[str, object], vars(subprocess)), 'run': FixtureExecutor(self.root).run})
            _ = self.enterContext(mock.patch.object(aws_cli, 'subprocess', process))

    # These tests retain child processes for environment, timeout, exit, and redaction checks.
    process_tests: ClassVar[frozenset[str]] = frozenset({
        'test_price_command_writes_private_state_for_the_selected_zone_and_admin_environment',
        'test_compute_timeout_keeps_exact_instance_costs_until_preparation_recovers',
        'test_terminal_update_failure_reconciles_compute_but_uncertain_failure_keeps_it_pending',
        'test_failure_redacts_the_prepared_address_after_the_private_file_changes',
        'test_update_failure_reports_current_events_and_rollback_without_the_address',
        'test_create_reservation_failures_report_events_reason_status_and_redact',
        'test_unconfirmed_scaler_key_is_deleted_and_never_printed',
        'test_admin_child_receives_only_the_environment_credentials',
        'test_admin_child_timeout_and_error_redact_secrets',
    })

    def test_admin_child_receives_only_the_environment_credentials(self) -> None:
        source = '''import json, os, sys
print(json.dumps({'key': os.environ.get('AWS_ACCESS_KEY_ID') == 'fixture-admin-id',
                  'secret': os.environ.get('AWS_SECRET_ACCESS_KEY') == 'fixture-admin-secret',
                  'config': os.environ.get('AWS_CONFIG_FILE'), 'profile': '--profile' in sys.argv,
                  'inherited_profile': 'AWS_PROFILE' in os.environ}))
'''
        self.fake_commands(f'#!{sys.executable}\n' + source, 'aws')
        execute = admin_secrets.AdminSecretSession(self.settings).executor(aws_target.execute_command)
        with mock.patch.dict(os.environ, {'AWS_PROFILE': 'unrelated'}):
            result = execute(['aws', '--profile', 'build-admin'], capture_output=True, text=True,
                             timeout=TEST_HANG_GUARD_SECS, check=True)
        self.assertEqual(json.loads(result.stdout), {'key': True, 'secret': True, 'config': '/dev/null',
                                                    'profile': False, 'inherited_profile': False})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, '')

    def test_admin_child_timeout_and_error_redact_secrets(self) -> None:
        session = admin_secrets.AdminSecretSession(self.settings)
        session.credentials = session.get()
        execute = session.executor(aws_target.execute_command)
        self.fake_commands(f'#!{sys.executable}\nimport time\ntime.sleep(5)\n', 'aws')
        with self.assertRaises(subprocess.TimeoutExpired) as timeout:
            _ = execute(['aws'], capture_output=True, text=True, timeout=0.02, check=False)
        self.assertEqual(cast(object, timeout.exception.cmd), 'administrator AWS command')
        self.assertIsNone(cast(object, timeout.exception.output))
        source = '''import os, sys
print(os.environ['AWS_ACCESS_KEY_ID'])
print('child refused ' + os.environ['AWS_SECRET_ACCESS_KEY'], file=sys.stderr)
sys.exit(7)
'''
        self.fake_commands(f'#!{sys.executable}\n' + source, 'aws')
        result = execute(['aws'], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, '', 'child refused [redacted]\n'))
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            _ = execute(['aws'], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=True)
        self.assertEqual(failure.exception.returncode, 7)
        self.assertEqual(cast(object, failure.exception.stderr), 'child refused [redacted]\n')
        self.assertIsNone(cast(object, failure.exception.output))

    def test_fixture_executor_preserves_command_records_output_and_environment(self) -> None:
        runner = FixtureExecutor(self.root)
        environment = dict(os.environ, FIXTURE_CHILD_ONLY='present')
        arguments = ['aws', '--region', 'us-east-1', 'sts', 'get-caller-identity']
        real = subprocess.run(arguments, capture_output=True, text=True, env=environment,
                              check=False, timeout=TEST_HANG_GUARD_SECS)
        record = (self.root / 'commands.jsonl').read_bytes()
        (self.root / 'commands.jsonl').unlink()
        argv = sys.argv
        before = dict(os.environ)
        result = runner.run(arguments, capture_output=True, text=True, env=environment,
                            check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (real.returncode, real.stdout, real.stderr))
        self.assertEqual((self.root / 'commands.jsonl').read_bytes(), record)
        self.assertEqual(dict(os.environ), before)
        self.assertIs(sys.argv, argv)
        self.fake_commands(f'#!{sys.executable}\nimport sys\nprint("failure", file=sys.stderr)\nsys.exit(3)\n', 'aws')
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            _ = runner.run(['aws'], capture_output=True, text=True, env=environment,
                           check=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(failure.exception.returncode, 3)
        self.assertEqual(cast(object, failure.exception.stderr), 'failure\n')
        self.assertEqual(dict(os.environ), before)
        self.assertIs(sys.argv, argv)

    @override
    def replace(self, key: str, value: str) -> None:
        if key in ('instance_id', 'existing_data_volume_id'):
            saved: dict[str, object] = aws_config.deployment_record(self.state)
            if not saved:
                saved = {'stack': 'previous-build', 'region': 'us-east-1',
                         'outputs': {'InstanceId': 'i-fixture'}}
            outputs = ledger.mapping(saved.get('outputs'))
            if key == 'instance_id':
                outputs['InstanceId'] = value
            else:
                outputs['ExistingDataInput'] = value
                outputs['DataVolumeId'] = value
            saved['outputs'] = outputs
            _ = (self.state / 'deployment.json').write_text(json.dumps(saved))
            return
        super().replace(key, value)

    def set_clock(self, at: int) -> None:
        self.now = at
        _ = (self.root / 'clock').write_text(str(at))


    def deployment(self) -> deploy.Deployment:
        return deploy.Deployment(self.settings, self.state, self.bundle, clock=lambda: self.now,
                                 notify=lambda _event: True)

    def previous_output(self, output: str, command: str) -> str:
        pending: list[str] = []
        previous: list[str] = []
        for line in output.splitlines():
            if not line.startswith(command + ': ') or line.startswith((command + ': refused:', command + ': image kept:')):
                previous.append(line)
                continue
            message = line.removeprefix(command + ': ')
            done = re.fullmatch(r'(.+) done in (\d+) s', message)
            heartbeat = re.fullmatch(r'still (.+), (\d+) min', message)
            if done:
                self.assertTrue(pending, line)
                self.assertEqual(pending.pop(), done[1])
            elif heartbeat:
                self.assertIn(heartbeat[1], pending)
            else:
                pending.append(message.removesuffix(' (can take several minutes)'))
        self.assertEqual(pending, [])
        return ''.join(line + '\n' for line in previous)

    def test_deployment_step_prints_start_and_elapsed_time(self) -> None:
        output = StringIO()
        with redirect_stdout(output), mock.patch.object(time, 'monotonic', side_effect=[10.0, 12.0]):
            with deploy.step('up', 'updating the stack', self.settings, hint='can take several minutes'):
                pass
        self.assertEqual(output.getvalue().splitlines(), [
            'up: updating the stack (can take several minutes)',
            'up: updating the stack done in 2 s'])

    def test_deployment_step_reports_failure_and_reraises(self) -> None:
        output = StringIO()
        with redirect_stdout(output), mock.patch.object(time, 'monotonic', side_effect=[10.0, 12.0]):
            with self.assertRaises(KeyboardInterrupt):
                with deploy.step('up', 'updating the stack', self.settings):
                    raise KeyboardInterrupt
        self.assertEqual(output.getvalue().splitlines(), [
            'up: updating the stack',
            'up: updating the stack failed after 2 s'])

    def test_deployment_step_prints_heartbeat_until_completion(self) -> None:
        self.replace('deploy_progress_interval_s', '0.001')
        heartbeat = threading.Event()

        class ProgressOutput(StringIO):
            @override
            def write(self, text: str) -> int:
                count = super().write(text)
                if 'up: still updating the stack,' in text:
                    heartbeat.set()
                return count

        output = ProgressOutput()
        with redirect_stdout(output):
            with deploy.step('up', 'updating the stack', self.settings, hint='can take several minutes'):
                self.assertTrue(heartbeat.wait(TEST_HANG_GUARD_SECS))
        self.assertRegex(output.getvalue(), r'up: still updating the stack, \d+ min\n')
        self.assertEqual(output.getvalue().splitlines()[0],
                         'up: updating the stack (can take several minutes)')
        self.assertRegex(output.getvalue().splitlines()[-1], r'^up: updating the stack done in \d+ s$')
        self.assertEqual(self.previous_output(output.getvalue(), 'up'), '')
        self.assertFalse(any(worker.name == 'deployment-progress' for worker in threading.enumerate()))

    def test_scaler_key_cleans_stale_and_unconfirmed_keys(self) -> None:
        subject = self.deployment()
        checkpoint = self.state / 'scaler-key.json'
        old_key = 'FIXTUREOLDKEY0000000'
        _ = checkpoint.write_text(json.dumps({'access_key_id': old_key}))
        with mock.patch.object(subject, '_scaler_present', return_value=False), \
                mock.patch.object(time, 'sleep', return_value=None):
            with self.assertRaisesRegex(deploy.DeploymentRefused, 'the new key was removed'):
                subject.scaler_key()
        deletes = [cast(list[str], row['args']) for row in self.commands('delete-access-key')]
        removed = [words[words.index('--access-key-id') + 1] for words in deletes]
        self.assertEqual(len(removed), 2)
        self.assertEqual(removed[0], old_key)
        self.assertNotEqual(removed[1], old_key)
        self.assertFalse(checkpoint.exists())
        self.assertEqual(self.credentials.read_text(), '')

    def test_stack_wait_uses_its_own_timeout_through_the_admin_executor(self) -> None:
        self.replace('stack_wait_timeout_s', '3600')
        calls: list[tuple[tuple[str, ...], float]] = []

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            calls.append((tuple(arguments), timeout))
            return aws_target.execute_command(arguments, capture_output=capture_output, text=text,
                                              timeout=timeout, check=check, env=env)

        subject = deploy.Deployment(self.settings, self.state, self.bundle, execute=execute,
                                    clock=lambda: self.now, notify=lambda _event: True)
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        waits = [(arguments, timeout) for arguments, timeout in calls if 'cloudformation' in arguments and 'wait' in arguments]
        self.assertTrue(waits)
        self.assertEqual({timeout for _, timeout in waits}, {3600})
        ordinary = [(arguments, timeout) for arguments, timeout in calls if 'wait' not in arguments]
        self.assertTrue(ordinary)
        self.assertEqual({timeout for _, timeout in ordinary}, {30})

    def test_secrets_command_prints_progress_without_exposing_credentials(self) -> None:
        arguments = ['deploy.py', 'secrets', '--settings', str(self.settings), '--directory', str(self.state),
                     '--output', str(self.bundle)]
        with (mock.patch.object(sys, 'argv', arguments), mock.patch.object(admin_secrets, 'configure') as configure,
              redirect_stdout(StringIO()) as output):
            self.assertEqual(deploy.main(), 0)
        configure.assert_called_once_with(self.settings)
        self.assertEqual(output.getvalue().splitlines()[0], 'secrets: saving encrypted credentials')
        self.assertEqual(self.previous_output(output.getvalue(), 'secrets'), '')

    def test_aws_trial_command_prints_progress_around_the_trial(self) -> None:
        arguments = ['deploy.py', 'aws-trial', '--base', 'base', '--changed', 'changed', '--modules', 'fixture/test_ok.py']
        with (mock.patch.object(sys, 'argv', arguments), mock.patch.object(aws_trial, 'main', return_value=0) as trial,
              redirect_stdout(StringIO()) as output):
            self.assertEqual(deploy.main(), 0)
        trial.assert_called_once_with(arguments[2:], conf.path('aws_target'), self.state)
        self.assertEqual(output.getvalue().splitlines()[0], 'aws-trial: running the trial')
        self.assertEqual(self.previous_output(output.getvalue(), 'aws-trial'), '')

    def commands(self, operation: str = '') -> list[dict[str, object]]:
        path = self.root / 'commands.jsonl'
        rows = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []
        if operation == 'deploy':
            # Compare parameters for both foundation deploys and inspected update executions.
            updates: dict[str, list[str]] = {}
            result: list[dict[str, object]] = []
            for row in rows:
                args = cast(list[str], row['args'])
                if 'create-change-set' in args and 'UPDATE' in args:
                    values = ledger.rows({'values': cast(object, json.loads(args[args.index('--parameters') + 1]))}, 'values')
                    updates[args[args.index('--change-set-name') + 1]] = [
                        f"{ledger.text(value, 'ParameterKey')}={value['ParameterValue']}"
                        for value in values if 'ParameterValue' in value]
                if 'deploy' in args:
                    result.append(row)
                elif 'execute-change-set' in args:
                    result.append({**row, 'args': [*args, *updates[args[args.index('--change-set-name') + 1]]]})
            return result
        return [row for row in rows if not operation or operation in cast(list[str], row['args'])]


    def failing_compute(self, failure: str) -> deploy.Deployment:
        compute_returned = False

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            nonlocal compute_returned
            operation = arguments[arguments.index('--region') + 3]
            if compute_returned and ((failure in ('stack', 'timeout') and operation == 'describe-stacks')
                                     or (failure == 'instance' and operation == 'describe-instances')):
                return subprocess.CompletedProcess(arguments, 1, '', 'the requested observation is unavailable')
            result = aws_target.execute_command(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
            if operation == 'execute-change-set':
                compute_returned = True
                if failure == 'timeout':
                    raise subprocess.TimeoutExpired(arguments, timeout)
            return result

        return deploy.Deployment(self.settings, self.state, self.bundle, execute=execute,
                                 clock=lambda: self.now, notify=lambda _event: True)

    def instance_intervals(self, at: int) -> list[tuple[int, int, Decimal, int]]:
        costs = ledger.costs_from_evidence(ledger.CostLedger(self.state).read_snapshot(),
                                           aws_config.read_settings(self.settings), at, current_window=False)
        return [(r.start, r.end, r.usd, r.confirmed_uptime_s) for r in costs.intervals if r.kind == 'instance']


    def update_fixture(self) -> deploy.Deployment:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.set_clock(self.now + 1)
        _ = subject.prepare()
        return subject

    def gate_image_fixture(self, failure: str = '') -> tuple[deploy.Deployment, GateImageAws]:
        subject = self.deployment()
        subject.directory = self.state / ('image-' + (failure or 'success'))
        subject.directory.mkdir()
        book = ledger.CostLedger(subject.directory)
        book.record_inventory(ledger.ResourceInventory(self.now - 1,
            (ledger.PricedResource('gate-shared', aws_config.money('1')),
             ledger.PricedResource('gate-image', aws_config.money('1.60'))), 'fixture', builder_removed=True))
        _ = (subject.directory / 'deployment.json').write_text(json.dumps({
            'stack_id': 'fixture-stack', 'stack': 'fixture-build'}))
        _ = (subject.directory / 'gate-deployment.json').write_text(json.dumps({
            'deployment_id': 'fixture', 'bucket': 'fixture-bucket', 'template': 'fixture-template',
            'version': '1', 'security_group': 'fixture-group', 'revision': 'f' * 40, 'image': 'ami-old',
            'subnets': [{'id': 'fixture-subnet', 'zone': 'fixture-zone'}]}))
        (subject.directory / 'gate-image').mkdir()
        _ = (subject.directory / 'gate-image/image.json').write_text('{"image_id":"ami-old"}')
        _ = (subject.directory / 'image.json').write_text('{"image_id":"ami-builder"}')
        fake = GateImageAws(failure)
        fake.manifest = subject.directory / 'gate-deployment.json'
        _ = self.enterContext(mock.patch.object(subject, '_api', return_value=aws_target.AwsCli(
            'fixture-admin', 'us-east-1', 1, execute=fake)))
        return subject, fake

    def test_account_apply_takes_the_account_stack_lock_before_the_hana_hold(self) -> None:
        subject = self.deployment()

        @contextmanager
        def take_hana_hold(directory: Path) -> Generator[HanaCommandAccess]:
            with account_stack_lock(directory) as account:
                self.assertIs(account, file_lock.LockResult.BUSY)
            with hana_command_lock(directory) as access:
                yield access

        def check_holds(*_arguments: object) -> None:
            with account_stack_lock(self.state) as account:
                self.assertIs(account, file_lock.LockResult.BUSY)
            with hana_command_lock(self.state) as hana:
                self.assertIs(hana, HanaCommandAccess.RESOURCES_HELD)
            raise ValueError('Fixture stops apply before AWS')

        with (mock.patch.object(deploy, 'hana_command_lock', new=take_hana_hold),
              mock.patch.object(subject, '_load', side_effect=check_holds) as load,
              self.assertRaisesRegex(ValueError, 'Fixture stops apply before AWS')):
            _ = subject.apply(approve_resources=True)
        load.assert_called_once()
        with account_stack_lock(self.state) as account, hana_command_lock(self.state) as hana:
            self.assertIs(account, file_lock.LockResult.ACQUIRED)
            self.assertIs(hana, HanaCommandAccess.ACQUIRED)

    def test_remove_gate_image_dry_run_needs_no_credentials_and_writes_nothing(self) -> None:
        self.credentials.unlink()
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with mock.patch.object(admin_secrets.AdminSecretSession, 'get') as credentials, redirect_stdout(StringIO()) as output:
            self.assertTrue(self.deployment().remove_images())
        credentials.assert_not_called()
        self.assertIn('remove-images --approve-destroy', output.getvalue())
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)

    def test_remove_gate_image_switches_to_the_official_image_before_deleting_the_gate_image(self) -> None:
        subject, fake = self.gate_image_fixture()
        with redirect_stdout(StringIO()) as output:
            self.assertTrue(subject.remove_images(approve_destroy=True))
        self.assertIn('remove-images: done; gates boot from the official image', output.getvalue())
        manifest = remote.read_record(fake.manifest)
        home = ledger.rows(manifest, 'regions')[0]
        self.assertEqual((home['image'], home['version'], manifest['revision']), ('ami-fixture', '2', 'f' * 40))
        self.assertEqual(len(fake.gate_changes), 2)
        for index, change in enumerate(fake.gate_changes):
            template = ledger.mapping(change['Template'])
            self.assertEqual(ledger.mapping(template['Resources'])['GateExpiryFunction'], {'live': 'keep'})
            image_policy = ledger.mapping(ledger.mapping(ledger.mapping(
                ledger.mapping(template['Resources'])['GateScalerPolicy'])['Properties'])['PolicyDocument'])
            statements = ledger.rows(image_policy, 'Statement')
            rows = ledger.rows(change, 'Parameters')
            self.assertEqual(next(row for row in rows if row['ParameterKey'] == 'VpcId'),
                             {'ParameterKey': 'VpcId', 'UsePreviousValue': True})
            image_parameter = next(row for row in rows if row['ParameterKey'] == 'GateImage')
            if index == 0:
                self.assertEqual(statements[-1], {'Sid': 'GateImageTransition', 'Effect': 'Allow',
                    'Action': ['ec2:RunInstances'], 'Resource': {
                        'Fn::Sub': 'arn:${AWS::Partition}:ec2:${AWS::Region}::image/ami-old'}})
                self.assertEqual(image_parameter, {'ParameterKey': 'GateImage', 'ParameterValue': 'ami-fixture'})
            else:
                self.assertEqual(len(statements), 1)
                self.assertEqual(template, fake.live_template)
                self.assertEqual(image_parameter, {'ParameterKey': 'GateImage', 'UsePreviousValue': True})
        self.assertEqual(fake.launchable_images(), {'ami-fixture'})
        operations = [args[args.index('--region') + 3] for args in fake.commands]
        last_change = len(operations) - operations[::-1].index('execute-change-set') - 1
        self.assertLess(last_change, operations.index('deregister-image'))
        self.assertLess(operations.index('deregister-image'), operations.index('delete-snapshot'))
        self.assertEqual((fake.images, fake.snapshots), ({}, {}))
        self.assertFalse((subject.directory / 'gate-image').exists())
        self.assertFalse((subject.directory / 'image.json').exists())
        self.assertIn('no saved image remains', output.getvalue())
        inventories = ledger.CostLedger(subject.directory).read_snapshot().inventories
        self.assertTrue(inventories[-1].builder_removed)
        self.assertEqual(inventories[-1].resources, (ledger.PricedResource('gate-shared', aws_config.money('1')),))
        # A second run finds nothing to change or delete.
        with redirect_stdout(StringIO()) as output:
            self.assertTrue(subject.remove_images(approve_destroy=True))
        self.assertIn('remove-images: nothing to remove', output.getvalue())
        self.assertEqual(len(fake.gate_changes), 2)

    def test_remove_images_preserves_regional_stacks_and_copied_images(self) -> None:
        subject, fake = self.gate_image_fixture()
        manifest = aws_config.gate_deployment(fake.manifest, 'us-east-1')
        regional: dict[str, object] = {'region': 'us-west-2', 'stack': 'fixture-west', 'image': 'ami-west'}
        manifest['regions'] = [*ledger.rows(manifest, 'regions'), regional]
        write_text_atomic(fake.manifest, json.dumps(manifest))
        records = {'regional-stacks.json': {'us-west-2': 'fixture-west'},
                   'regional-images.json': {'copy': {'region': 'us-west-2', 'image': 'ami-west'}}}
        for name, record in records.items():
            _ = (subject.directory / name).write_text(json.dumps(record))
        before = {name: (subject.directory / name).read_bytes() for name in records}
        with redirect_stdout(StringIO()):
            self.assertTrue(subject.remove_images(approve_destroy=True))
        self.assertEqual(ledger.rows(remote.read_record(fake.manifest), 'regions')[1], regional)
        self.assertEqual({name: (subject.directory / name).read_bytes() for name in records}, before)
        self.assertTrue(all(command[command.index('--region') + 1] == 'us-east-1' for command in fake.commands))

    def test_remove_gate_image_reads_events_when_the_update_wait_fails(self) -> None:
        subject, fake = self.gate_image_fixture('waiter')
        with redirect_stdout(StringIO()), self.assertRaisesRegex(aws_target.AwsUnavailable, 'fixture stack waiter failure'):
            _ = subject.remove_images(approve_destroy=True)
        operations = [command[command.index('--region') + 3] for command in fake.commands]
        self.assertEqual(operations[-2:], ['wait', 'describe-stack-events'])
        self.assertIn('snap-old', fake.snapshots)

    def test_remove_gate_image_keeps_the_selected_image_launchable_when_a_step_fails(self) -> None:
        for failure in ('activate', 'waiter', 'manifest', 'narrow waiter'):
            with self.subTest(failure=failure):
                subject, fake = self.gate_image_fixture(failure)
                atomic_write = write_text_atomic
                def write(path: Path, text: str) -> None:
                    if path == fake.manifest and fake.failure == 'manifest':
                        raise OSError('fixture manifest write failure')
                    atomic_write(path, text)
                with mock.patch.object(deploy, 'write_text_atomic', side_effect=write), redirect_stdout(StringIO()):
                    with self.assertRaises((aws_target.AwsUnavailable, OSError)):
                        _ = subject.remove_images(approve_destroy=True)
                    selected = ledger.text(ledger.rows(remote.read_record(fake.manifest), 'regions')[0], 'image')
                    self.assertEqual(selected, 'ami-fixture' if failure == 'narrow waiter' else 'ami-old')
                    self.assertIn(selected, fake.launchable_images())
                    self.assertIn('snap-old', fake.snapshots)
                    self.assertTrue((subject.directory / 'gate-image').exists())
                    fake.failure = ''
                    self.assertTrue(subject.remove_images(approve_destroy=True))
                self.assertEqual(fake.launchable_images(), {'ami-fixture'})
                self.assertNotIn('snap-old', fake.snapshots)
                self.assertFalse((subject.directory / 'gate-image').exists())

    def gate_fixture(self, change: str = 'GateBucket', *, probe_faults: Mapping[str, str | tuple[str, ...]] | None = None,
                     has_gpu_target: bool = True, unoffered_gpu_type: str = '',
                     termination_decision: str = 'allowed', denied_region: str = '',
                     resource_specific_simulation: bool = False,
                     scaler_arn: str = 'arn:aws:iam::fixture-account:user/fixture-scaler',
                     regional_stacks: dict[str, dict[str, str]] | None = None
                     ) -> tuple[deploy.Deployment, list[list[str]]]:
        subject = self.update_fixture()
        self.set_clock(self.now + 1)
        self.configure_gate('instance_types=m7a.8xlarge m7a.4xlarge\n'
            + 'hana_gpu_instance_types=g4dn.2xlarge g6.2xlarge\n'
            + 'spot_candidate_regions=us-east-2\n'
            + 'gpu_setup_dry_run_retry_timeout_s=1\ngpu_setup_dry_run_retry_interval_s=1')
        text_setting(self.settings, 'region', 'us-east-2')
        text_setting(self.settings, 'availability_zone', 'us-east-2a')
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2')
        commands: list[list[str]] = []
        outputs = {'GateBucket': 'fixture-bucket', 'GateLaunchTemplate': 'fixture-template',
                   'GateLaunchVersion': '1', 'GateSecurityGroup': 'fixture-group', 'GateRoleId': 'fixture-role',
                   'GateProfileArn': 'fixture-profile', 'ScalerUserName': 'fixture-scaler'}
        faults = probe_faults or {}
        fault_attempts: dict[str, int] = {}
        probe_bodies: dict[str, bytes] = {}
        artifact_key = 'artifacts/gpu-size-trial/setup-probe.json'
        input_key = 'requests/gpu-size-trial/setup-probe.json'
        upload_id = 'fixture-upload'

        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            commands.append(args)
            region_index = args.index('--region')
            region = args[region_index + 1]
            operation = args[region_index + 3]
            reply: dict[str, object] = {}
            if regional_stacks is not None and region in regional_stacks and operation == 'describe-stacks':
                regional_stack = regional_stacks[region]
                if not regional_stack.get('StackStatus'):
                    return subprocess.CompletedProcess(args, 255, '', 'An error occurred (ValidationError): Stack does not exist')
                regional_outputs = {'GateLaunchTemplate': 'fixture-' + region, 'GateSecurityGroup': 'fixture-group',
                                    'GateLaunchVersion': regional_stack.get('launch_version', '1')}
                reply = {'Stacks': [{**regional_stack, 'Outputs': [
                    {'OutputKey': key, 'OutputValue': value} for key, value in regional_outputs.items()]}]}
            elif regional_stacks is not None and region in regional_stacks and operation == 'deploy':
                regional_stack = regional_stacks[region]
                if regional_stack.get('StackStatus'):
                    regional_stack['StackStatus'] = 'UPDATE_COMPLETE'
                    _ = regional_stack.setdefault('LastUpdatedTime', '2026-01-15T12:00:00Z')
                else:
                    regional_stack['StackStatus'] = 'CREATE_COMPLETE'
                _ = regional_stack.setdefault('StackId', 'fixture-stack-' + region)
            elif operation == 'describe-stacks':
                reply = {'Stacks': [{'Parameters': [{'ParameterKey': key} for key in
                    ('VpcId', 'NotificationEmail')]}]}
            elif operation == 'describe-route-tables':
                reply = {'RouteTables': [{'Associations': [{'Main': True}], 'Routes': [
                    {'DestinationCidrBlock': '0.0.0.0/0', 'State': 'active', 'GatewayId': 'igw-fixture'}]}]}
            elif operation == 'describe-subnets':
                reply = {'Subnets': [{'SubnetId': 'fixture-' + zone, 'AvailabilityZone': zone, 'State': 'available'}
                                    for zone in ('a', 'b')]}
            elif operation == 'get-caller-identity':
                reply = {'Account': 'fixture-account'}
            elif operation == 'describe-change-set':
                reply = {'Status': 'CREATE_COMPLETE', 'Changes': [{'ResourceChange': {'LogicalResourceId': change}}]}
            elif operation == 'simulate-principal-policy':
                self.assertEqual(args[:7], ['aws', '--profile', 'build-admin', '--region', 'us-east-1',
                                            'iam', 'simulate-principal-policy'])
                region = args[12].split(':')[3]
                self.assertEqual(args[7:13], ['--policy-source-arn', 'arn:aws:iam::fixture-account:user/fixture-scaler',
                    '--action-names', 'ec2:TerminateInstances', '--resource-arns',
                    f'arn:aws:ec2:{region}:fixture-account:instance/i-00000000000000000'])
                self.assertEqual(args[13], '--context-entries')
                saved = remote.read_record(self.state / 'gate-deployment.json')
                self.assertEqual(json.loads(args[14]), [
                    {'ContextKeyName': 'ec2:ResourceTag/GateDeployment', 'ContextKeyType': 'string',
                     'ContextKeyValues': [saved['deployment_id']]},
                    {'ContextKeyName': 'ec2:ResourceTag/Workload', 'ContextKeyType': 'string',
                     'ContextKeyValues': ['cloud-claude-gate']},
                    {'ContextKeyName': 'aws:RequestedRegion', 'ContextKeyType': 'string',
                     'ContextKeyValues': [region]},
                ])
                self.assertEqual(args[15:], ['--no-paginate', '--output', 'json'])
                if faults.get(operation) == 'AccessDenied':
                    return subprocess.CompletedProcess(args, 1, '',
                        'An error occurred (AccessDenied) when calling the SimulatePrincipalPolicy operation: private diagnostic')
                decision = 'explicitDeny' if region == denied_region else termination_decision
                evaluation: dict[str, object] = {'EvalActionName': 'ec2:TerminateInstances',
                    'EvalResourceName': args[12], 'EvalDecision': decision}
                if resource_specific_simulation:
                    evaluation['EvalResourceName'] = 'arn:${Partition}:ec2:${Region}:${Account}:instance/${InstanceId}'
                    evaluation['ResourceSpecificResults'] = [
                        {'EvalResourceName': args[12], 'EvalResourceDecision': decision}]
                reply = {'EvaluationResults': [evaluation]}
            elif operation not in ('create-change-set', 'execute-change-set', 'delete-change-set', 'wait'):
                raise AssertionError(operation)
            return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')

        _ = self.enterContext(mock.patch.object(subject, '_api', return_value=aws_target.AwsCli(
            'build-admin', 'us-east-1', 1, execute=execute)))
        _ = self.enterContext(mock.patch.object(subject, '_stack', return_value=outputs))
        _ = self.enterContext(mock.patch.object(aws_trial, 'host_nixpkgs_revision', return_value='fixture-revision'))

        def scaler(arguments: Sequence[str], *, env: dict[str, str] | None = None,
                   **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            commands.append(args)
            profile = conf.read(self.settings).text('credentials_profile')
            self.assertEqual(args[:5], ['aws', '--profile', profile, '--region', 'us-east-2'])
            self.assertEqual({key: value for key, value in (env or {}).items() if key.startswith('AWS_')}, {
                'AWS_SHARED_CREDENTIALS_FILE': str(self.credentials), 'AWS_EC2_METADATA_DISABLED': 'true'})
            saved = remote.read_record(self.state / 'gate-deployment.json')
            service, operation = args[5:7]
            dry_run = operation in ('run-instances', 'terminate-instances')
            self.assertEqual('--dry-run' in args, dry_run)
            reply: dict[str, object] = {}
            name = operation
            if operation == 'get-caller-identity':
                self.assertEqual(service, 'sts')
                self.assertEqual(args[7:], ['--output', 'json'])
                reply = {'Arn': scaler_arn}
            elif operation == 'describe-instances':
                self.assertEqual(service, 'ec2')
                self.assertIn('Name=instance-lifecycle,Values=spot', args)
                tags = [{'Key': 'Workload', 'Value': 'cloud-claude-gate'}, {'Key': 'GpuSizeTrial', 'Value': 'fixture-trial'}]
                instances: list[dict[str, object]] = [{'InstanceId': 'fixture-foreign-box',
                    'Tags': [*tags, {'Key': 'GateDeployment', 'Value': 'another-deployment'}]}]
                if has_gpu_target:
                    instances.append({'InstanceId': 'fixture-gpu-box',
                        'Tags': [*tags, {'Key': 'GateDeployment', 'Value': saved['deployment_id']}]})
                reply = {'Reservations': [{'Instances': instances}]}
            elif operation in ('describe-instance-type-offerings', 'describe-spot-price-history'):
                self.assertEqual(service, 'ec2')
                if operation == 'describe-instance-type-offerings':
                    types = args[-3].removeprefix('Name=instance-type,Values=').split(',')
                    reply = {'InstanceTypeOfferings': [{'InstanceType': size,
                        'Location': 'c' if size == unoffered_gpu_type else 'b' if size == 'g4dn.2xlarge' else 'a'}
                        for size in types]}
            elif operation == 'get-service-quota':
                self.assertEqual(service, 'service-quotas')
                self.assertIn('L-3819A6DF', args)
                reply = {'Quota': {'Value': 48}}
            elif operation == 'get-cost-and-usage':
                self.assertEqual(service, 'ce')
                self.assertEqual(args[9:17], ['--granularity', 'DAILY', '--metrics', 'UnblendedCost',
                    'UsageQuantity', '--group-by', 'Type=DIMENSION,Key=USAGE_TYPE', '--filter'])
                self.assertIn('--no-paginate', args)
                reply = account_reply(args)
            elif operation == 'export-credentials':
                self.assertEqual(service, 'configure')
                self.assertEqual(args[7:9], ['--format', 'process'])
                reply = {'AccessKeyId': 'fixture-key', 'SecretAccessKey': 'fixture-secret'}
            elif operation == 'run-instances':
                self.assertEqual(service, 'ec2')
                launch = ledger.mapping(cast(object, json.loads(args[args.index('--cli-input-json') + 1])))
                name += ' ' + ledger.text(launch, 'InstanceType')
            elif operation == 'terminate-instances':
                self.assertEqual(service, 'ec2')
                self.assertTrue(has_gpu_target)
                self.assertEqual(args[7:9], ['--instance-ids', 'fixture-gpu-box'])
            elif operation in ('put-object', 'list-objects-v2', 'get-object'):
                self.assertEqual(service, 's3api')
                probe_key = args[10]
                self.assertIn(probe_key, (artifact_key, input_key))
                if probe_key == input_key:
                    name += ' inputs'
                self.assertEqual(args[7:11], ['--bucket', outputs['GateBucket'],
                    '--prefix' if operation == 'list-objects-v2' else '--key', probe_key])
                if operation == 'put-object':
                    probe_bodies[probe_key] = Path(args[args.index('--body') + 1]).read_bytes()
                    self.assertTrue(probe_bodies[probe_key])
                elif operation == 'list-objects-v2':
                    self.assertEqual(probe_key, artifact_key)
                    self.assertEqual(args[11:14], ['--max-keys', '1', '--no-paginate'])
                    reply = {'Contents': [{'Key': probe_key}]} if probe_key in probe_bodies else {}
                else:
                    _ = Path(args[-3]).write_bytes(probe_bodies[probe_key])
            elif operation in ('create-multipart-upload', 'abort-multipart-upload'):
                self.assertEqual(service, 's3api')
                self.assertEqual(args[7:11], ['--bucket', outputs['GateBucket'], '--key', input_key])
                if operation == 'create-multipart-upload':
                    reply = {'UploadId': upload_id}
                else:
                    self.assertEqual(args[11:13], ['--upload-id', upload_id])
            else:
                raise AssertionError(operation)
            default_code = 'DryRunOperation' if dry_run else ''
            scripted = faults.get(name, default_code)
            if isinstance(scripted, tuple):
                position = fault_attempts.get(name, 0)
                fault_attempts[name] = position + 1
                code = scripted[position] if position < len(scripted) else default_code
            else:
                code = scripted
            if code == 'TimeoutExpired':
                raise subprocess.TimeoutExpired(operation, 1)
            if code:
                action = ''.join(word.title() for word in operation.split('-'))
                return subprocess.CompletedProcess(args, 1, '',
                    f'An error occurred ({code}) when calling the {action} operation: private diagnostic '
                    + 'https://fixture-bucket.example/fixture-key arn:aws:ec2:us-east-2:fixture-account:instance/i-fixture')
            return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')

        subject.execute = scaler
        # A gate deploy reads prices at its end; the fixture keeps that read local.
        _ = self.enterContext(mock.patch.object(deploy, 'read_price_snapshot', return_value=0))
        return subject, commands

    def test_gate_up_reads_prices_at_once_after_the_stack_update(self) -> None:
        subject, _commands = self.gate_fixture()
        output = StringIO()
        with mock.patch.object(deploy, 'read_price_snapshot', return_value=0) as fill, redirect_stdout(output):
            self.assertTrue(subject.gate_up(approve_resources=True))
        fill.assert_called_once()
        self.assertEqual(fill.call_args.args, (self.settings, self.state))
        self.assertIs(cast(bool, fill.call_args.kwargs['force']), True)
        # The deploy shows the reasons, so it sends no alert; the hourly refresh still can.
        notify = cast(Callable[[str, object, float], bool], fill.call_args.kwargs['notify'])
        self.assertFalse(notify('fixture-key', object(), 1.0))
        self.assertIn('cloud-gate: Spot prices and quotas read', output.getvalue())

    def test_failed_price_read_retries_and_keeps_the_deploy(self) -> None:
        subject, _commands = self.gate_fixture()
        pauses: list[float] = []
        subject.pause = pauses.append
        output = StringIO()
        with mock.patch.object(deploy, 'read_price_snapshot', return_value=1) as fill, redirect_stdout(output):
            self.assertTrue(subject.gate_up(approve_resources=True))
        configuration = conf.read(self.settings)
        attempts = configuration.whole('deploy_price_fill_attempts')
        self.assertEqual(fill.call_count, attempts)
        self.assertEqual(pauses, [configuration.number('deploy_price_fill_pause_s')] * (attempts - 1))
        self.assertIn('the Spot price read failed', output.getvalue())

    def test_price_read_error_names_its_reason_and_keeps_the_deploy(self) -> None:
        subject, _commands = self.gate_fixture()
        output = StringIO()
        with mock.patch.object(deploy, 'read_price_snapshot', side_effect=OSError('fixture lock refusal')), redirect_stdout(output):
            self.assertTrue(subject.gate_up(approve_resources=True))
        self.assertIn('the Spot price read did not run: OSError: fixture lock refusal', output.getvalue())
        self.assertIn('the Spot price read failed', output.getvalue())

    def test_gate_up_records_regional_stack_after_home_stack(self) -> None:
        subject, commands = self.gate_fixture()
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2 us-west-2')
        west: dict[str, object] = {'region': 'us-west-2', 'image': 'ami-west', 'subnets': [],
            'template': 'lt-west', 'version': '1', 'security_group': 'sg-west', 'stack': 'fixture-west'}
        network = deploy.RegionalNetwork('us-west-2', 'vpc-fixture-west', [], 'ami-west')
        def regional(_api: aws_target.AwsCli, _settings: aws_config.DeploymentSettings, _home: dict[str, str],
                     _parameters: dict[str, str], given: deploy.RegionalNetwork,
                     _digests: deploy.RegionalStackDigests) -> dict[str, object]:
            self.assertTrue(any('execute-change-set' in command for command in commands))
            self.assertEqual(given, network)
            return west
        with mock.patch.object(subject, 'regional_network', return_value=network), \
                mock.patch.object(subject, 'regional_gate_stack', side_effect=regional):
            self.assertTrue(subject.gate_up(approve_resources=True))
        change_set = next(command for command in commands if 'create-change-set' in command)
        passed = {ledger.text(row, 'ParameterKey'): row for row in
                  ledger.rows({'rows': json.loads(change_set[change_set.index('--parameters') + 1])}, 'rows')}
        known = ledger.mapping(ledger.mapping(cast(object, json.loads(deploy.TEMPLATE.read_text())))['Parameters'])
        self.assertEqual(set(passed) - set(known), set())
        self.assertNotIn('GateSubnets', passed)
        vpcs = ledger.text(passed['GateVpcs'], 'ParameterValue').split(',')
        self.assertEqual([vpc.split(':')[3] for vpc in vpcs], ['us-east-2', 'us-west-2'])
        self.assertTrue(vpcs[1].endswith(':vpc/vpc-fixture-west'))
        self.assertTrue(ledger.text(passed['GateRegionImages'], 'ParameterValue').endswith(':ec2:us-west-2::image/ami-west'))
        saved = remote.read_record(self.state / 'gate-deployment.json')
        self.assertEqual([row['region'] for row in ledger.rows(saved, 'regions')], ['us-east-2', 'us-west-2'])
        self.assertEqual(ledger.rows(saved, 'regions')[1], west)
        self.assertEqual(saved['instance_types'], ['m7a.8xlarge', 'm7a.4xlarge'])

    def test_gate_up_deploys_regions_without_digests_and_reads_live_outputs_on_skip(self) -> None:
        stacks = {region: {'StackStatus': 'UPDATE_COMPLETE'} for region in ('us-west-2', 'eu-north-1')}
        subject, commands = self.gate_fixture(regional_stacks=stacks)
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2 ' + ' '.join(stacks))
        settings = aws_config.read_deployment_settings(self.settings)
        names = {region: settings.stack_name + '-gates-' + region for region in stacks}
        write_text_atomic(self.state / 'regional-stacks.json', json.dumps(names))

        def network(_api: aws_target.AwsCli, _settings: aws_config.DeploymentSettings, region: str) -> deploy.RegionalNetwork:
            return deploy.RegionalNetwork(region, 'fixture-vpc', [], 'fixture-image')

        with mock.patch.object(subject, 'regional_network', side_effect=network), redirect_stdout(StringIO()) as output:
            self.assertTrue(subject.gate_up(approve_resources=True))
            self.assertEqual([command[command.index('--stack-name') + 1] for command in commands if 'deploy' in command],
                             list(names.values()))
            digests = remote.read_record(self.state / 'regional-stack-digests.json')
            self.assertEqual(set(digests), set(stacks))
            for region, stack in stacks.items():
                entry = ledger.mapping(digests[region])
                self.assertRegex(ledger.text(entry, 'digest'), r'^[0-9a-f]{64}$')
                self.assertEqual(entry['StackId'], stack['StackId'])
                self.assertEqual(entry['LastUpdatedTime'], stack['LastUpdatedTime'])
            self.assertEqual(remote.read_record(self.state / 'regional-stacks.json'), names)
            commands.clear()
            _ = output.seek(0)
            _ = output.truncate()
            for stack in stacks.values():
                stack['launch_version'] = '2'
            self.assertTrue(subject.gate_up(approve_resources=True))

        self.assertFalse(any('deploy' in command for command in commands))
        for name in names.values():
            self.assertEqual(sum('describe-stacks' in command and name in command for command in commands), 1)
        regions = ledger.rows(remote.read_record(self.state / 'gate-deployment.json'), 'regions')
        self.assertEqual([row['version'] for row in regions[1:]], ['2', '2'])
        for region in stacks:
            self.assertEqual(output.getvalue().count(f'cloud-gate: {region}: gate stack unchanged; skipped'), 1)
            self.assertRegex(output.getvalue(), rf'cloud-gate: {region}: updating the gate stack done in \d+ s')
        timings = ledger.mapping(remote.read_record(self.state / 'gate-region-times.json')['seconds'])
        self.assertEqual(set(ledger.mapping(timings['stack'])), set(stacks))

    def test_gate_up_deploys_only_regions_whose_inputs_change(self) -> None:
        stacks: dict[str, dict[str, str]] = {'us-west-2': {}, 'eu-north-1': {}}
        subject, commands = self.gate_fixture('GateGpuTrialScalerPolicy', regional_stacks=stacks)
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2 ' + ' '.join(stacks))
        images = {region: 'fixture-image' for region in stacks}
        template = self.root / 'cloudformation.json'
        source = deploy.TEMPLATE.read_text()
        write_text_atomic(template, source)

        def network(_api: aws_target.AwsCli, _settings: aws_config.DeploymentSettings, region: str) -> deploy.RegionalNetwork:
            return deploy.RegionalNetwork(region, 'fixture-vpc', [], images[region])

        def deployed() -> list[str]:
            return [command[command.index('--region') + 1] for command in commands if 'deploy' in command]

        with (mock.patch.object(subject, 'regional_network', side_effect=network),
              mock.patch.object(deploy, 'TEMPLATE', template), redirect_stdout(StringIO())):
            self.assertTrue(subject.gate_up(approve_resources=True))
            self.assertEqual(deployed(), list(stacks))

            commands.clear()
            home_change = source.replace('"ec2:DescribeInstanceTypeOfferings",',
                                         '"ec2:DescribeInstanceTypeOfferings", "ec2:DescribeInstanceTypes",', 1)
            self.assertNotEqual(home_change, source)
            write_text_atomic(template, home_change)
            self.assertTrue(subject.gate_up(approve_resources=True))
            self.assertTrue(any('execute-change-set' in command for command in commands))
            self.assertEqual(deployed(), [])

            commands.clear()
            images['us-west-2'] = 'fixture-new-image'
            self.assertTrue(subject.gate_up(approve_resources=True))
            self.assertEqual(deployed(), ['us-west-2'])
            self.assertIn('GateImage=fixture-new-image', next(command for command in commands if 'deploy' in command))

            commands.clear()
            regional_change = home_change.replace('"GateExpiryFunction": {',
                '"GateExpiryFunction": {"Metadata": {"Fixture": "regional change"},', 1)
            self.assertNotEqual(regional_change, home_change)
            write_text_atomic(template, regional_change)
            self.assertTrue(subject.gate_up(approve_resources=True))
            self.assertEqual(deployed(), list(stacks))

    def test_gate_up_redeploys_when_the_stack_is_missing_or_ineligible(self) -> None:
        region = 'us-west-2'
        stack: dict[str, str] = {}
        subject, commands = self.gate_fixture(regional_stacks={region: stack})
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2 ' + region)
        network = deploy.RegionalNetwork(region, 'fixture-vpc', [], 'fixture-image')
        with mock.patch.object(subject, 'regional_network', return_value=network), redirect_stdout(StringIO()):
            self.assertTrue(subject.gate_up(approve_resources=True))
            for status in ('UPDATE_ROLLBACK_COMPLETE', 'UPDATE_IN_PROGRESS', ''):
                with self.subTest(status=status):
                    stack['StackStatus'] = status
                    commands.clear()
                    self.assertTrue(subject.gate_up(approve_resources=True))
                    self.assertEqual(sum('deploy' in command for command in commands), 1)

    def test_gate_up_redeploys_when_the_stack_id_or_update_time_changes(self) -> None:
        region = 'us-west-2'
        stack = {'StackStatus': 'UPDATE_COMPLETE'}
        subject, commands = self.gate_fixture(regional_stacks={region: stack})
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2 ' + region)
        network = deploy.RegionalNetwork(region, 'fixture-vpc', [], 'fixture-image')
        path = self.state / 'regional-stack-digests.json'
        with mock.patch.object(subject, 'regional_network', return_value=network), redirect_stdout(StringIO()):
            self.assertTrue(subject.gate_up(approve_resources=True))
            previous = ledger.mapping(remote.read_record(path)[region])
            for key, value in (('StackId', 'fixture-replaced-stack'), ('LastUpdatedTime', '2026-01-16T12:00:00Z')):
                with self.subTest(key=key):
                    stack[key] = value
                    commands.clear()
                    self.assertTrue(subject.gate_up(approve_resources=True))
                    self.assertEqual(sum('deploy' in command for command in commands), 1)
                    saved = ledger.mapping(remote.read_record(path)[region])
                    self.assertEqual(saved, {**previous, key: value})
                    previous = saved
            commands.clear()
            self.assertTrue(subject.gate_up(approve_resources=True))
            self.assertFalse(any('deploy' in command for command in commands))

    def test_gate_up_warns_once_and_deploys_all_regions_when_the_digest_file_is_unusable(self) -> None:
        stacks: dict[str, dict[str, str]] = {'us-west-2': {}, 'eu-north-1': {}}
        subject, commands = self.gate_fixture(regional_stacks=stacks)
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2 ' + ' '.join(stacks))
        path = self.state / 'regional-stack-digests.json'

        def network(_api: aws_target.AwsCli, _settings: aws_config.DeploymentSettings, region: str) -> deploy.RegionalNetwork:
            return deploy.RegionalNetwork(region, 'fixture-vpc', [], 'fixture-image')

        with mock.patch.object(subject, 'regional_network', side_effect=network), redirect_stdout(StringIO()):
            self.assertTrue(subject.gate_up(approve_resources=True))
            partly_invalid = remote.read_record(path)
            partly_invalid['eu-north-1'] = {'digest': 'broken'}
            for contents in (json.dumps(partly_invalid), '', '[]', None):
                with self.subTest(contents=contents):
                    if contents is None:
                        path.unlink()
                        path.mkdir()
                    else:
                        write_text_atomic(path, contents)
                    commands.clear()
                    with redirect_stdout(StringIO()) as output:
                        self.assertTrue(subject.gate_up(approve_resources=True))
                    deployed = [command[command.index('--region') + 1] for command in commands if 'deploy' in command]
                    self.assertEqual(deployed, list(stacks))
                    self.assertEqual(output.getvalue().count('regional-stack-digests.json'), 1)
                    if contents is not None:
                        self.assertEqual(set(remote.read_record(path)), set(stacks))

    def test_gate_up_prints_regional_progress_and_uses_saved_times(self) -> None:
        subject, _commands = self.gate_fixture()
        regions = ('us-west-2', 'eu-north-1')
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2 ' + ' '.join(regions))
        clock = [0.0]
        path = self.state / 'gate-region-times.json'
        network_action = 'preparing the network and image'
        stack_action = 'updating the gate stack'
        network_seconds = (2, 4)
        stack_seconds = (160, 170)
        output = StringIO()

        def completed(key: str, action: str, region: str, durations: tuple[int, int]) -> None:
            index = regions.index(region)
            self.assertIn(f'cloud-gate: {region}: {action} ({index + 1} of 2)', output.getvalue())
            if index:
                saved = ledger.mapping(remote.read_record(path)['seconds'])
                self.assertEqual(ledger.mapping(saved[key])[regions[0]], durations[0])
            clock[0] += durations[index]

        def network(_api: aws_target.AwsCli, _settings: aws_config.DeploymentSettings, region: str) -> deploy.RegionalNetwork:
            completed('network', network_action, region, network_seconds)
            return deploy.RegionalNetwork(region, 'fixture-vpc', [], 'fixture-image')

        def stack(_api: aws_target.AwsCli, _settings: aws_config.DeploymentSettings, _home: dict[str, str],
                  _parameters: dict[str, str], given: deploy.RegionalNetwork,
                  _digests: deploy.RegionalStackDigests) -> dict[str, object]:
            completed('stack', stack_action, given.region, stack_seconds)
            return {'region': given.region}

        for run in range(2):
            output = StringIO()
            with (mock.patch.object(subject, 'regional_network', side_effect=network),
                    mock.patch.object(subject, 'regional_gate_stack', side_effect=stack),
                    mock.patch.object(time, 'monotonic', side_effect=lambda: clock[0]), redirect_stdout(output)):
                self.assertTrue(subject.gate_up(approve_resources=True))
            lines = output.getvalue().splitlines()
            previous = -1
            for action, durations, total in ((network_action, network_seconds, 6), (stack_action, stack_seconds, 330)):
                estimate = f'expected total {total} s from saved regional times' if run else 'expected total is not yet measured'
                expected = [f'cloud-gate: {action} in 2 regions: {estimate}']
                for index, region in enumerate(regions):
                    expected.extend((f'cloud-gate: {region}: {action} ({index + 1} of 2)',
                                     f'cloud-gate: {region}: {action} done in {durations[index]} s'))
                for line in expected:
                    position = lines.index(line)
                    self.assertGreater(position, previous)
                    previous = position
            self.assertEqual(remote.read_record(path), {'version': 1, 'seconds': {
                'network': dict(zip(regions, network_seconds)), 'stack': dict(zip(regions, stack_seconds))}})
            network_seconds = (3, 5)
            stack_seconds = (150, 180)

    def test_regional_time_estimate_uses_the_median_for_an_unmeasured_region(self) -> None:
        path = self.state / 'gate-region-times.json'
        _ = path.write_text('{"version": 1, "seconds": {"stack": {"us-west-2": 120, "eu-north-1": 180}}}')
        timings = deploy.GateRegionTimings.read(self.state)
        output = StringIO()
        with redirect_stdout(output):
            timings.announce(deploy.GateRegionOperation.STACK, ('us-west-2', 'eu-north-1', 'us-east-1'))
        self.assertEqual(output.getvalue(),
                         'cloud-gate: updating the gate stack in 3 regions: expected total 450 s from saved regional times\n')

    def test_invalid_regional_times_do_not_supply_an_estimate(self) -> None:
        path = self.state / 'gate-region-times.json'
        for contents in ('{', '{}', '{"version": 2}',
                         '{"version": 1, "seconds": {"stack": {"us-west-2": -1}}}',
                         '{"version": 1, "seconds": {"stack": {"us-west-2": true}}}',
                         '{"version": 1, "seconds": {"stack": {"us-west-2": NaN}}}'):
            with self.subTest(contents=contents):
                _ = path.write_text(contents)
                with redirect_stdout(StringIO()) as output:
                    deploy.GateRegionTimings.read(self.state).announce(deploy.GateRegionOperation.STACK, ('us-west-2',))
                self.assertEqual(output.getvalue(),
                    'cloud-gate: updating the gate stack in 1 regions: expected total is not yet measured\n')

    def test_regional_time_overflow_does_not_supply_an_estimate(self) -> None:
        path = self.state / 'gate-region-times.json'
        _ = path.write_text(json.dumps({'version': 1, 'seconds': {'stack': {'us-west-2': 10**400}}}))
        with redirect_stdout(StringIO()) as output:
            deploy.GateRegionTimings.read(self.state).announce(deploy.GateRegionOperation.STACK, ('us-west-2',))
        self.assertEqual(output.getvalue(),
            'cloud-gate: updating the gate stack in 1 regions: expected total is not yet measured\n')

    def test_gate_up_warns_and_continues_when_it_cannot_write_regional_times(self) -> None:
        subject, _commands = self.gate_fixture()
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2 us-west-2')
        (self.state / 'gate-region-times.json').mkdir()
        network = deploy.RegionalNetwork('us-west-2', 'fixture-vpc', [], 'fixture-image')
        with (mock.patch.object(subject, 'regional_network', return_value=network),
                mock.patch.object(subject, 'regional_gate_stack', return_value={'region': network.region}),
                redirect_stdout(StringIO()) as output):
            self.assertTrue(subject.gate_up(approve_resources=True))
        warnings = [line for line in output.getvalue().splitlines() if 'warning:' in line]
        self.assertEqual(warnings, ['cloud-gate: warning: could not write gate-region-times.json'] * 2)
        manifest = remote.read_record(self.state / 'gate-deployment.json')
        self.assertEqual([row['region'] for row in ledger.rows(manifest, 'regions')], ['us-east-2', 'us-west-2'])

    def test_gate_up_saves_each_successful_region_before_a_later_region_fails(self) -> None:
        subject, _commands = self.gate_fixture()
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2 us-west-2 eu-north-1')
        def network(_api: aws_target.AwsCli, _settings: aws_config.DeploymentSettings, region: str) -> deploy.RegionalNetwork:
            return deploy.RegionalNetwork(region, 'fixture-vpc', [], 'fixture-image')
        def stack(_api: aws_target.AwsCli, _settings: aws_config.DeploymentSettings, _home: dict[str, str],
                  _parameters: dict[str, str], given: deploy.RegionalNetwork,
                  _digests: deploy.RegionalStackDigests) -> dict[str, object]:
            if given.region == 'eu-north-1':
                raise aws_target.AwsUnavailable('fixture regional failure')
            return {'region': given.region}
        with (mock.patch.object(subject, 'regional_network', side_effect=network),
                mock.patch.object(subject, 'regional_gate_stack', side_effect=stack), redirect_stdout(StringIO())):
            with self.assertRaisesRegex(aws_target.AwsUnavailable, 'fixture regional failure'):
                _ = subject.gate_up(approve_resources=True)
        saved = ledger.mapping(remote.read_record(self.state / 'gate-region-times.json')['seconds'])
        self.assertEqual(set(ledger.mapping(saved['network'])), {'us-west-2', 'eu-north-1'})
        self.assertEqual(set(ledger.mapping(saved['stack'])), {'us-west-2'})
        manifest = remote.read_record(self.state / 'gate-deployment.json')
        self.assertEqual([row['region'] for row in ledger.rows(manifest, 'regions')], ['us-east-2', 'us-west-2'])

    def test_prices_lists_all_region_type_zone_combinations_without_ids(self) -> None:
        subject = self.deployment()
        text_setting(self.settings, 'region', 'us-east-2')
        text_setting(self.settings, 'availability_zone', 'us-east-2a')
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2')
        text_setting(self.root / 'cloud_gate.conf', 'spot_candidate_regions', 'us-west-2 us-east-1 us-east-2 us-west-2')
        gpu_types = ('g6.xlarge', 'g4dn.xlarge', 'g4ad.xlarge', 'g5.xlarge')
        # The fixture names its own types, so a tuned live value never changes this test.
        text_setting(self.root / 'cloud_gate.conf', 'instance_types', 'm7a.8xlarge')
        text_setting(self.root / 'cloud_gate.conf', 'gpu_instance_types', ' '.join(gpu_types))
        price_regions: list[str] = []
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            region = args[args.index('--region') + 1]
            if 'get-service-quota' in args:
                code = args[args.index('--quota-code') + 1]
                self.assertIn(code, ('L-34B43A08', 'L-3819A6DF'))
                reply: dict[str, object] = {'Quota': {'Value': 48 if code == 'L-3819A6DF' else 128}}
            elif 'describe-availability-zones' in args:
                reply = {'AvailabilityZones': [{'ZoneName': region + 'a'}]}
            else:
                self.assertIn('describe-spot-price-history', args)
                price_regions.append(region)
                sizes = args[args.index('--instance-types') + 1:args.index('--product-descriptions')]
                self.assertTrue(set(gpu_types).issubset(sizes))
                self.assertEqual(len(sizes), len(set(sizes)))
                reply = {'SpotPriceHistory': [
                    {'InstanceType': size, 'AvailabilityZone': region + 'a', 'SpotPrice': '.25', 'Timestamp': '2026-01-15T00:00:00Z'}
                    for size in ('m7a.8xlarge', *gpu_types)]}
            return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')
        with (mock.patch.object(subject, '_api', return_value=aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute)),
              redirect_stdout(StringIO()) as output):
            subject.prices()
        lines = output.getvalue().splitlines()
        self.assertEqual(price_regions, ['us-east-2', 'us-west-2', 'us-east-1'])
        self.assertIn('us-east-2 m7a.8xlarge us-east-2a 0.25', lines)
        for region in ('us-east-2', 'us-west-2', 'us-east-1'):
            self.assertIn(f'{region}: Spot quota 128 vCPUs', lines)
            self.assertEqual(lines[lines.index(f'{region}: Spot quota 128 vCPUs') + 1],
                             f'{region}: G and VT Spot quota 48 vCPUs')
            for size in ('r7a.8xlarge', 'm8a.8xlarge', 'c8a.8xlarge'):
                self.assertIn(f'{region} {size} {region}a unavailable', lines)
            for size in gpu_types:
                self.assertIn(f'{region} {size} {region}a 0.25', lines)
        self.assertTrue(all('ami-' not in line and 'arn:' not in line for line in lines))

    def test_prices_and_up_report_regional_quotas_through_the_admin_executor(self) -> None:
        cases: tuple[tuple[str, dict[str, int | str], int], ...] = (
            ('m7a.4xlarge m7a.8xlarge', {'us-east-1': 32}, 32),
            ('m7a.4xlarge m7a.8xlarge', {'us-east-1': 16}, 32),
            ('m7a.4xlarge m7a.8xlarge', {'us-east-1': 'AccessDenied: fixture-admin-secret'}, 32),
            ('m7a.4xlarge m7a.8xlarge', {'us-east-1': 64, 'us-west-2': 32}, 32),
            ('m7a.4xlarge m7a.8xlarge', {'us-east-1': 'AccessDenied: fixture-admin-secret', 'us-west-2': 64}, 32),
            ('m7a.4xlarge m7a.8xlarge', {'us-east-1': 'timeout', 'us-west-2': 32}, 32),
            ('m7a.xlarge', {'us-east-1': 2, 'us-west-2': 4}, 4),
            ('m7a.large', {'us-east-1': 1, 'us-west-2': 2}, 2),
            ('m7a', {'us-east-1': 1, 'us-west-2': 32}, 0),
            ('m7a.metal', {'us-east-1': 1, 'us-west-2': 32}, 0),
            ('m7a.unknown', {'us-east-1': 1, 'us-west-2': 32}, 0),
            ('m7a m7a.metal m7a.large m7a.8xlarge m7a.xlarge', {'us-east-1': 16, 'us-west-2': 32}, 32))
        for command in ('prices',):
            for instance_types, quotas, largest in cases:
                with self.subTest(command=command, quotas=quotas, instance_types=instance_types):
                    gates = self.root / 'cloud_gate.conf'
                    text_setting(gates, 'regions', ' '.join(quotas))
                    text_setting(gates, 'spot_candidate_regions', ' '.join(quotas))
                    text_setting(gates, 'instance_types', instance_types)
                    subject = self.deployment()
                    subject.admin.credentials = subject.admin.get()
                    calls: list[tuple[str, str]] = []
                    execute = subject.execute

                    def quota_execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                                      timeout: float, check: bool,
                                      env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                        region = arguments[arguments.index('--region') + 1] if '--region' in arguments else ''
                        if 'get-service-quota' in arguments:
                            code = arguments[arguments.index('--quota-code') + 1]
                            self.assertNotIn('--profile', arguments)
                            self.assertIsNotNone(env)
                            self.assertEqual(cast(dict[str, str], env)['AWS_ACCESS_KEY_ID'], 'fixture-admin-id')
                            self.assertEqual(cast(dict[str, str], env)['AWS_SECRET_ACCESS_KEY'], 'fixture-admin-secret')
                            self.assertEqual(list(arguments[arguments.index('service-quotas'):]),
                                ['service-quotas', 'get-service-quota', '--service-code', 'ec2',
                                 '--quota-code', code, '--output', 'json'])
                            calls.append((region, code))
                            if code == 'L-3819A6DF':
                                return subprocess.CompletedProcess(arguments, 0, json.dumps({'Quota': {'Value': 48}}), '')
                            self.assertEqual(code, 'L-34B43A08')
                            quota = quotas[region]
                            if quota == 'timeout':
                                raise subprocess.TimeoutExpired('fixture-admin-secret', timeout)
                            return subprocess.CompletedProcess(arguments, 1 if isinstance(quota, str) else 0,
                                '' if isinstance(quota, str) else json.dumps({'Quota': {'Value': quota}}),
                                quota if isinstance(quota, str) else '')
                        if 'describe-availability-zones' in arguments:
                            return subprocess.CompletedProcess(arguments, 0,
                                json.dumps({'AvailabilityZones': [{'ZoneName': region + 'a'}]}), '')
                        if 'describe-spot-price-history' in arguments and '--availability-zone' not in arguments:
                            return subprocess.CompletedProcess(arguments, 0, json.dumps({'SpotPriceHistory': []}), '')
                        self.assertNotIn('describe-instance-types', arguments)
                        return execute(arguments, capture_output=capture_output, text=text,
                                       timeout=timeout, check=check, env=env)

                    subject.execute = quota_execute
                    with mock.patch.object(deploy, 'largest_gate_vcpus', wraps=deploy.largest_gate_vcpus) as count_vcpus:
                        with redirect_stdout(StringIO()) as captured:
                            subject.prices()
                        output = captured.getvalue()
                        self.assertIn('us-east-1 r7a.8xlarge us-east-1a unavailable', output)
                        count_vcpus.assert_called_once_with(tuple(instance_types.split()))
                    codes = ('L-34B43A08', 'L-3819A6DF') if command == 'prices' else ('L-34B43A08',)
                    self.assertEqual(calls, [(region, code) for region in quotas for code in codes])
                    self.assertEqual([line for line in output.splitlines() if ': G and VT' in line],
                                     [f'{region}: G and VT Spot quota 48 vCPUs' for region in quotas]
                                     if command == 'prices' else [])
                    lines = [line for line in output.splitlines()
                             if ': Spot' in line or ': cannot run' in line or ': gate vCPU' in line]
                    expected: list[str] = []
                    for region, quota in quotas.items():
                        if isinstance(quota, str):
                            line = next(line for line in lines if line.startswith(region + ': Spot vCPU quota unknown:'))
                            if quota == 'timeout':
                                self.assertIn('timed out', line)
                            else:
                                self.assertIn('AccessDenied: [redacted]', line)
                            expected.append(line)
                        else:
                            expected.append(f'{region}: Spot quota {quota} vCPUs')
                            if largest == 0:
                                expected.append(f'{region}: gate vCPU count is unknown')
                            elif quota < largest:
                                expected.append(f'{region}: cannot run one gate ({largest} vCPUs); ask AWS for a Spot vCPU quota increase in this region.')
                    self.assertEqual(lines, expected)
                    self.assertNotIn('fixture-admin-secret', output)

    def test_prices_prints_whole_quotas_and_the_header_after_the_passphrase_prompt(self) -> None:
        text_setting(self.root / 'cloud_gate.conf', 'spot_candidate_regions', 'us-east-1')
        subject = self.deployment()
        credentials = subject.admin.get()
        prompted = False

        def unlock() -> admin_secrets.AdminCredentials:
            nonlocal prompted
            if not prompted:
                print('Admin passphrase: ')
                prompted = True
            return credentials

        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            if 'get-service-quota' in arguments:
                reply: dict[str, object] = {'Quota': {'Value': 300.0}}
            elif 'describe-availability-zones' in arguments:
                reply = {'AvailabilityZones': []}
            else:
                reply = {'SpotPriceHistory': []}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

        subject.execute = execute
        with mock.patch.object(subject.admin, 'get', side_effect=unlock), redirect_stdout(StringIO()) as captured:
            subject.prices()
        lines = captured.getvalue().splitlines()
        self.assertEqual(lines, ['Admin passphrase: ', 'region type zone price', 'us-east-1: Spot quota 300 vCPUs',
                                'us-east-1: G and VT Spot quota 300 vCPUs'])

    def test_prices_continues_after_a_failed_g_quota_read_without_error_details(self) -> None:
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-1 us-west-2')
        for failure, reason in (('denied', 'AccessDeniedException'), ('timeout', 'TimeoutExpired'), ('malformed', 'KeyError')):
            with self.subTest(failure=failure):
                subject = self.deployment()

                def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    args = list(arguments)
                    region = args[args.index('--region') + 1]
                    reply: dict[str, object]
                    if 'get-service-quota' in args:
                        code = args[args.index('--quota-code') + 1]
                        if code == 'L-3819A6DF' and region == 'us-east-1':
                            if failure == 'denied':
                                return subprocess.CompletedProcess(args, 1, '',
                                    'An error occurred (AccessDeniedException) when calling GetServiceQuota: '
                                    + 'fixture-admin-secret arn:aws:iam::123456789012:user/private')
                            if failure == 'timeout':
                                raise subprocess.TimeoutExpired('fixture-admin-secret', 1)
                            reply = {}
                        else:
                            reply = {'Quota': {'Value': 48 if code == 'L-3819A6DF' else 128}}
                    elif 'describe-availability-zones' in args:
                        reply = {'AvailabilityZones': [{'ZoneName': region + 'a'}]}
                    else:
                        self.assertIn('describe-spot-price-history', args)
                        reply = {'SpotPriceHistory': []}
                    return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')

                subject.execute = execute
                with redirect_stdout(StringIO()) as output:
                    subject.prices()
                lines = output.getvalue().splitlines()
                self.assertIn(f'us-east-1: G and VT Spot quota unknown: {reason}', lines)
                self.assertIn('us-west-2: G and VT Spot quota 48 vCPUs', lines)
                self.assertIn('us-west-2 r7a.8xlarge us-west-2a unavailable', lines)
                self.assertNotIn('fixture-admin-secret', output.getvalue())
                self.assertNotIn('arn:', output.getvalue())
                self.assertNotIn('123456789012', output.getvalue())

    def test_quota_status_reads_with_plan_key_and_prints_newest_request_without_ids(self) -> None:
        gates = self.root / 'cloud_gate.conf'
        home = conf.read(self.settings).text('region')
        other, candidate = 'us-west-2', 'eu-north-1'
        text_setting(gates, 'regions', home + ' ' + other)
        text_setting(gates, 'spot_candidate_regions', other + ' ' + candidate)
        text_setting(gates, 'gpu_size_trial_spot_max_usd_per_hour', '{"g6.8xlarge": "1"}')
        applied = {'L-34B43A08': 120, 'L-3819A6DF': 32}
        day_ago = datetime.fromtimestamp(self.now - 86400, UTC)
        hours_ago = datetime.fromtimestamp(self.now - 10800, UTC).isoformat()
        history = 'list-requested-service-quota-change-history-by-quota'
        calls: list[tuple[str, str, str]] = []
        subject = self.deployment()

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            self.assertTrue(capture_output and text)
            self.assertFalse(check)
            self.assertGreater(timeout, 0)
            self.assertNotIn('--profile', arguments)
            assert env is not None
            self.assertEqual(env['AWS_ACCESS_KEY_ID'], QUOTA_PLAN_KEY.access_key_id)
            self.assertEqual(env['AWS_SECRET_ACCESS_KEY'], QUOTA_PLAN_KEY.secret_access_key)
            self.assertEqual(env['AWS_SHARED_CREDENTIALS_FILE'], '/dev/null')
            self.assertEqual(env['AWS_CONFIG_FILE'], '/dev/null')
            self.assertNotIn('AWS_PROFILE', env)
            self.assertNotIn('AWS_SESSION_TOKEN', env)
            region = arguments[arguments.index('--region') + 1]
            operation = arguments[arguments.index('service-quotas') + 1]
            code = arguments[arguments.index('--quota-code') + 1]
            self.assertIn(operation, ('get-service-quota', history))
            self.assertEqual(list(arguments), ['aws', '--region', region, 'service-quotas', operation,
                '--service-code', 'ec2', '--quota-code', code, '--output', 'json'])
            calls.append((region, code, operation))
            reply: dict[str, object]
            if operation == 'get-service-quota':
                reply = {'Quota': {'Value': applied[code], 'QuotaName': 'private-name', 'QuotaArn': 'arn:private-quota'}}
            else:
                rows: list[dict[str, object]] = []
                if (region, code) == (home, 'L-34B43A08'):
                    rows = [{'DesiredValue': 128, 'Status': 'APPROVED', 'Created': day_ago.timestamp(),
                             'LastUpdated': self.now},
                            {'DesiredValue': 129, 'Status': 'CASE_OPENED', 'CaseId': 'case-fixture',
                             'Created': hours_ago, 'LastUpdated': hours_ago,
                             'Id': 'private-request', 'Requester': '123456789012', 'QuotaArn': 'arn:private-quota'}]
                elif (region, code) == (other, 'L-34B43A08'):
                    rows = [{'DesiredValue': 130, 'Status': 'FUTURE_STATUS', 'Created': hours_ago},
                            {'DesiredValue': 128, 'Status': 'PENDING', 'Created': day_ago.timestamp()}]
                elif (region, code) == (other, 'L-3819A6DF'):
                    rows = [{'DesiredValue': 4, 'Status': 'PENDING',
                             'LastUpdated': day_ago.isoformat().replace('+00:00', 'Z')}]
                reply = {'RequestedQuotas': rows}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

        subject.execute = execute
        before = {path.relative_to(self.root): path.read_bytes() if path.is_file() else None
                  for path in self.root.rglob('*')}
        with (mock.patch.dict(os.environ, {'AWS_SESSION_TOKEN': 'ambient-token', 'AWS_PROFILE': 'ambient-profile'}),
              mock.patch.object(plan_identity, 'plan_credentials', return_value=QUOTA_PLAN_KEY) as plan_key,
              mock.patch.object(subject.admin, 'get', side_effect=AssertionError('The report must not read the admin key')),
              mock.patch.object(admin_secrets, 'credentials_file', side_effect=AssertionError('The report must not load the admin path')),
              mock.patch.object(deploy, 'Deployment', return_value=subject),
              mock.patch.object(sys, 'argv', ['deploy.py', 'quota-status', '--settings', str(self.settings)]),
              mock.patch.object(deploy, 'write_text_atomic', side_effect=AssertionError('The report must not write')),
              mock.patch.object(deploy, 'write_bytes_atomic', side_effect=AssertionError('The report must not write')),
              mock.patch.object(subject, '_guard', side_effect=AssertionError('The report must not change the ledger')),
              redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors):
            self.assertEqual(deploy.main(), 0, errors.getvalue())
        plan_key.assert_called_once_with()
        after = {path.relative_to(self.root): path.read_bytes() if path.is_file() else None
                 for path in self.root.rglob('*')}
        self.assertEqual(before, after)
        # The candidate region has only a price read, so the report reads no quota there.
        self.assertEqual(calls, [(region, code, operation) for region, code in (
            (home, 'L-34B43A08'), (home, 'L-3819A6DF'), (other, 'L-34B43A08'), (other, 'L-3819A6DF'))
            for operation in ('get-service-quota', history)])
        self.assertEqual(output.getvalue().splitlines(), [
            home, '  Standard Spot: 120 vCPUs applied, 128 vCPUs needed',
            '    Newest request: CASE_OPENED for 129 vCPUs (3h old).',
            '  G and VT Spot: 32 vCPUs applied, 4 vCPUs needed', '    No requests.', '',
            other, '  Standard Spot: 120 vCPUs applied, 128 vCPUs needed',
            '    Newest request: FUTURE_STATUS for 130 vCPUs (3h old).',
            '  G and VT Spot: 32 vCPUs applied, 4 vCPUs needed',
            '    Newest request: PENDING for 4 vCPUs (1d old).', '',
            f'Summary: GPU trial needs 32 vCPUs; quota fits: {home}, {other}; '
            + f'open requests: {home} Standard Spot 129 vCPUs (CASE_OPENED), '
            + f'{other} Standard Spot 130 vCPUs (FUTURE_STATUS), '
            + f'{other} Standard Spot 128 vCPUs (PENDING), '
            + f'{other} G and VT Spot 4 vCPUs (PENDING)'])
        for private in ('private-name', 'private-request', 'case-fixture', '123456789012', 'arn:',
                        'fixture-admin-id', 'fixture-admin-secret', QUOTA_PLAN_KEY.access_key_id,
                        QUOTA_PLAN_KEY.secret_access_key):
            self.assertNotIn(private, output.getvalue() + errors.getvalue())

    def test_quota_status_continues_after_regional_failures_and_exits_one(self) -> None:
        gates = self.root / 'cloud_gate.conf'
        home = conf.read(self.settings).text('region')
        other = 'us-west-2'
        text_setting(gates, 'regions', home + ' ' + other)
        text_setting(gates, 'gpu_size_trial_spot_max_usd_per_hour', '{"g6.8xlarge": "1"}')
        history = 'list-requested-service-quota-change-history-by-quota'
        cases = (
            ('get-service-quota', 'AccessDeniedException'), (history, 'AccessDeniedException'),
            ('timeout', ''), ('uncoded', ''),
            ('get-service-quota', 'InvalidClientTokenId'), ('get-service-quota', 'UnrecognizedClientException'),
            ('get-service-quota', 'AuthFailure'), ('get-service-quota', 'SignatureDoesNotMatch'),
            (history, 'AuthFailure'),
        )
        for failure, error_code in cases:
            with self.subTest(failure=failure, error_code=error_code):
                subject = self.deployment()
                calls: list[tuple[str, str, str]] = []

                def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                            timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    _ = capture_output, text, check, env
                    region = arguments[arguments.index('--region') + 1]
                    operation = arguments[arguments.index('service-quotas') + 1]
                    code = arguments[arguments.index('--quota-code') + 1]
                    self.assertIn(operation, ('get-service-quota', history))
                    calls.append((region, code, operation))
                    if region == home and code == 'L-34B43A08':
                        if failure == 'timeout':
                            raise subprocess.TimeoutExpired('private-timeout', timeout)
                        if failure == 'uncoded' or operation == failure:
                            action = 'GetServiceQuota' if operation == 'get-service-quota' else 'ListRequestedServiceQuotaChangeHistoryByQuota'
                            reason = 'private-error' if failure == 'uncoded' else (
                                f'An error occurred ({error_code}) when calling the {action} operation: private-error arn:private')
                            return subprocess.CompletedProcess(arguments, 1, '', reason)
                    reply: dict[str, object] = {'Quota': {'Value': 128}}
                    if operation == history:
                        reply = {'RequestedQuotas': []}
                    return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

                subject.execute = execute
                with (mock.patch.object(plan_identity, 'plan_credentials', return_value=QUOTA_PLAN_KEY),
                      mock.patch.object(deploy, 'Deployment', return_value=subject),
                      mock.patch.object(sys, 'argv', ['deploy.py', 'quota-status', '--settings', str(self.settings)]),
                      redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors):
                    self.assertEqual(deploy.main(), 1)
                if error_code and error_code != 'AccessDeniedException':
                    expected_calls = [(home, 'L-34B43A08', 'get-service-quota')]
                    if failure == history:
                        expected_calls.append((home, 'L-34B43A08', history))
                    self.assertEqual(calls, expected_calls)
                    self.assertEqual(output.getvalue(), '')
                    self.assertEqual(errors.getvalue(), 'quota-status: AWS rejected the plan key. '
                                                       + 'Run deploy.py plan-identity --check.\n')
                    continue
                self.assertEqual(errors.getvalue(), '')
                expected = error_code or ('PlanRefused' if failure == 'timeout' else 'AwsUnavailable')
                self.assertIn(f'    Quota status failed: {expected}', output.getvalue())
                self.assertIn((other, 'L-34B43A08', history), calls)
                self.assertIn((other, 'L-3819A6DF', history), calls)
                self.assertIn((home, 'L-3819A6DF', history), calls)
                summary = output.getvalue().splitlines()[-1]
                self.assertTrue(summary.startswith('Summary: GPU trial needs '))
                self.assertIn('request history unavailable: ' + home + ' Standard Spot', summary)
                self.assertIn('open requests: unknown', summary)
                for private in ('private-error', 'private-timeout', 'arn:'):
                    self.assertNotIn(private, output.getvalue() + errors.getvalue())

    def test_quota_status_summary_uses_the_trial_types_and_ignores_closed_requests(self) -> None:
        gates = self.root / 'cloud_gate.conf'
        home = conf.read(self.settings).text('region')
        text_setting(gates, 'regions', home)
        applied = 4
        for size, needed in (('g4dn.xlarge', 4), ('g6.8xlarge', 32)):
            with self.subTest(size=size):
                text_setting(gates, 'gpu_size_trial_spot_max_usd_per_hour', json.dumps({size: '1'}))
                subject = self.deployment()
                def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    operation = arguments[arguments.index('service-quotas') + 1]
                    if operation == 'get-service-quota':
                        reply: dict[str, object] = {'Quota': {'Value': applied}}
                    else:
                        self.assertEqual(operation, 'list-requested-service-quota-change-history-by-quota')
                        reply = {'RequestedQuotas': [{'DesiredValue': 4, 'Status': status}
                            for status in ('APPROVED', 'DENIED', 'CASE_CLOSED', 'NOT_APPROVED', 'INVALID_REQUEST')]}
                    return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
                subject.execute = execute
                with (mock.patch.object(plan_identity, 'plan_credentials', return_value=QUOTA_PLAN_KEY),
                      redirect_stdout(StringIO()) as output):
                    self.assertTrue(subject.quota_status())
                self.assertIn('Newest request: APPROVED for 4 vCPUs (age unknown).', output.getvalue())
                fits = home if applied >= needed else 'no region has enough applied quota'
                self.assertEqual(output.getvalue().splitlines()[-1],
                    f'Summary: GPU trial needs {needed} vCPUs; quota fits: {fits}; open requests: none')

    def test_quota_status_summary_separates_unread_quotas_and_unread_history(self) -> None:
        gates = self.root / 'cloud_gate.conf'
        home = conf.read(self.settings).text('region')
        other = 'us-west-2'
        text_setting(gates, 'regions', home + ' ' + other)
        text_setting(gates, 'gpu_size_trial_spot_max_usd_per_hour', '{"g6.8xlarge": "1"}')
        needed = 32
        history = 'list-requested-service-quota-change-history-by-quota'
        for failure in ('get-service-quota', history):
            with self.subTest(failure=failure):
                subject = self.deployment()
                def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    region = arguments[arguments.index('--region') + 1]
                    operation = arguments[arguments.index('service-quotas') + 1]
                    code = arguments[arguments.index('--quota-code') + 1]
                    self.assertIn(operation, ('get-service-quota', history))
                    if region == home and code == 'L-3819A6DF' and operation == failure:
                        return subprocess.CompletedProcess(arguments, 1, '', 'fixture refusal')
                    reply: dict[str, object] = {'Quota': {'Value': needed}}
                    if operation == history:
                        reply = {'RequestedQuotas': [{'DesiredValue': needed, 'Status': 'PENDING'}]}
                    return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
                subject.execute = execute
                with (mock.patch.object(plan_identity, 'plan_credentials', return_value=QUOTA_PLAN_KEY),
                      redirect_stdout(StringIO()) as output):
                    self.assertFalse(subject.quota_status())
                fits = f'{other}, unknown for {home}' if failure == 'get-service-quota' else f'{home}, {other}'
                self.assertEqual(output.getvalue().splitlines()[-1],
                    f'Summary: GPU trial needs {needed} vCPUs; quota fits: {fits}; open requests: unknown; '
                    + f'request history unavailable: {home} G and VT Spot')
                self.assertIn('PENDING', output.getvalue())

    def test_quota_status_omits_identifiers_from_a_closed_request(self) -> None:
        gates = self.root / 'cloud_gate.conf'
        text_setting(gates, 'gpu_size_trial_spot_max_usd_per_hour', '{"g6.xlarge": "1"}')
        subject = self.deployment()

        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            operation = arguments[arguments.index('service-quotas') + 1]
            reply: dict[str, object]
            if operation == 'get-service-quota':
                reply = {'Quota': {'Value': 4}}
            else:
                self.assertEqual(operation, 'list-requested-service-quota-change-history-by-quota')
                reply = {'RequestedQuotas': [{'DesiredValue': 4, 'Status': 'CASE_CLOSED', 'CaseId': 'case-private',
                    'Id': 'request-private', 'Requester': '123456789012', 'Created': self.now - 120,
                    'LastUpdated': 'invalid date'}]}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

        subject.execute = execute
        with (mock.patch.object(plan_identity, 'plan_credentials', return_value=QUOTA_PLAN_KEY),
              redirect_stdout(StringIO()) as output):
            self.assertTrue(subject.quota_status())
        for identity in ('case-private', 'request-private', '123456789012'):
            self.assertNotIn(identity, output.getvalue())
        self.assertIn('    Newest request: CASE_CLOSED for 4 vCPUs (2m old).', output.getvalue())

    def test_quota_status_credential_refusal_stops_before_aws(self) -> None:
        plan_key = self.root / 'plan.gpg'
        for locked, message in (
            (False, 'The AWS plan key is missing. On natedev, run deploy.py plan-identity --approve-resources.'),
            (True, 'The AWS plan key is locked. Run github-warmup in a terminal on natedev.'),
        ):
            with self.subTest(locked=locked):
                if locked:
                    _ = plan_key.write_bytes(b'fixture-plan-key')
                    plan_key.chmod(0o600)
                subject = self.deployment()
                refusal = subprocess.CompletedProcess(['gpg'], 2, b'', b'[GNUPG:] ERROR pkdecrypt_failed 85\n')
                with (mock.patch.object(plan_identity, 'credential_path', return_value=plan_key),
                      mock.patch('scripts.aws_target.plan_identity._gpg', return_value=refusal),
                      mock.patch.object(subject.admin, 'get',
                                        side_effect=admin_secrets.SecretsRefused('Run deploy.py secrets in a terminal')) as admin_key,
                      mock.patch.object(subject, 'execute', side_effect=AssertionError('The refusal must stop AWS calls')),
                      mock.patch.object(deploy, 'Deployment', return_value=subject),
                      mock.patch.object(sys, 'argv', ['deploy.py', 'quota-status', '--settings', str(self.settings)]),
                      redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors):
                    self.assertEqual(deploy.main(), 1)
                self.assertEqual(output.getvalue(), '')
                self.assertEqual(errors.getvalue(), 'quota-status: ' + message + '\n')
                admin_key.assert_not_called()

    def test_quota_request_without_an_environment_override_reaches_aws_with_the_configured_file(self) -> None:
        gates = self.root / 'cloud_gate.conf'
        text_setting(gates, 'credential_file', str(self.credentials))
        expected = str(Path(conf.read(gates).text('credential_file')).expanduser())
        subject = self.deployment()
        calls: list[str] = []

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            self.assertTrue(capture_output and text)
            self.assertFalse(check)
            self.assertGreater(timeout, 0)
            self.assertIsNotNone(env)
            self.assertEqual(cast(dict[str, str], env)['AWS_SHARED_CREDENTIALS_FILE'], expected)
            self.assertIn('get-service-quota', arguments)
            calls.append(arguments[arguments.index('--region') + 1])
            return subprocess.CompletedProcess(arguments, 0, json.dumps({'Quota': {'Value': 300}}), '')

        subject.execute = execute
        with (mock.patch.dict(os.environ), mock.patch.object(deploy, 'Deployment', return_value=subject),
              mock.patch.object(sys, 'argv', ['deploy.py', 'quota-request', '--settings', str(self.settings)]),
              redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors):
            _ = os.environ.pop('AWS_SHARED_CREDENTIALS_FILE', None)
            self.assertEqual(deploy.main(), 0, errors.getvalue())
            self.assertEqual(os.environ['AWS_SHARED_CREDENTIALS_FILE'], expected)
        self.assertTrue(calls)

    def test_quota_request_prints_one_credential_refusal_and_stops_before_aws(self) -> None:
        message = 'Run deploy.py secrets in a terminal to restore the encrypted credentials'
        for error in (admin_secrets.SecretsRefused(message), deploy.DeploymentRefused(message)):
            with self.subTest(error=type(error).__name__):
                subject = self.deployment()
                with (mock.patch.object(subject.admin, 'get', side_effect=error) as get,
                      mock.patch.object(subject, 'execute', side_effect=AssertionError('The refusal must stop AWS calls')) as execute,
                      mock.patch.object(deploy, 'Deployment', return_value=subject),
                      mock.patch.object(sys, 'argv', ['deploy.py', 'quota-request', '--settings', str(self.settings)]),
                      redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors):
                    self.assertEqual(deploy.main(), 1)
                get.assert_called_once_with()
                execute.assert_not_called()
                self.assertEqual(output.getvalue(), '')
                self.assertEqual(errors.getvalue(), 'aws_deploy: ' + message + '\n')

    def test_quota_request_uses_admin_credentials_and_continues_after_regional_failures(self) -> None:
        get_quota = 'get-service-quota'
        history = 'list-requested-service-quota-change-history-by-quota'
        request = 'request-service-quota-increase'
        cases = (
            (300.0, '', '', '', True),
            (128, '', '', '', True),
            (5, 'PENDING', '', '', True),
            (5, 'CASE_OPENED', '', '', True),
            (5, 'IN_PROGRESS', '', '', True),
            (5, '', 'PENDING', '', True),
            (5, 'APPROVED', 'CASE_OPENED', '', True),
            (5, 'DENIED', 'PENDING', '', True),
            (5, 'CASE_CLOSED', 'PENDING', '', True),
            (5, 'NOT_APPROVED', 'PENDING', '', True),
            (5, 'INVALID_REQUEST', 'PENDING', '', True),
            (5, '', '', get_quota, False),
            (5, '', '', history, False),
            (5, '', '', request, False),
            (5, '', '', 'timeout', False),
            (5, '', '', 'uncoded', False),
            (5, '', 'NOT_APPROVED', '', False))
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^regions=.*$', 'regions=us-east-1 us-west-2',
                                  re.sub(r'(?m)^spot_candidate_regions=.*$',
                                         'spot_candidate_regions=us-east-2 us-west-2', path.read_text())))
        request_id = 'req-ab12cd34ef56'
        case_id = '9876543210'
        account_id = '123456789012'
        for quota, history_status, request_status, failure, success in cases:
            with self.subTest(quota=quota, history=history_status, request=request_status, failure=failure):
                subject = self.deployment()
                subject.admin.credentials = subject.admin.get()
                calls: list[tuple[str, str]] = []
                gpu_regions: list[str] = []

                def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                            timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    self.assertTrue(capture_output)
                    self.assertTrue(text)
                    self.assertFalse(check)
                    region = arguments[arguments.index('--region') + 1]
                    operation = arguments[arguments.index('service-quotas') + 1]
                    self.assertNotIn('--profile', arguments)
                    self.assertIsNotNone(env)
                    self.assertEqual(cast(dict[str, str], env)['AWS_ACCESS_KEY_ID'], 'fixture-admin-id')
                    self.assertEqual(cast(dict[str, str], env)['AWS_SECRET_ACCESS_KEY'], 'fixture-admin-secret')
                    if arguments[arguments.index('--quota-code') + 1] == 'L-3819A6DF':
                        self.assertEqual(operation, get_quota)
                        gpu_regions.append(region)
                        return subprocess.CompletedProcess(arguments, 0, json.dumps({'Quota': {'Value': 4}}), '')
                    expected = ['service-quotas', operation, '--service-code', 'ec2', '--quota-code', 'L-34B43A08']
                    if operation == request:
                        expected.extend(['--desired-value', '128'])
                    self.assertEqual(list(arguments[arguments.index('service-quotas'):]), [*expected, '--output', 'json'])
                    calls.append((region, operation))
                    if region == 'us-west-2':
                        self.assertEqual(operation, get_quota)
                        reply: dict[str, object] = {'Quota': {'Value': 300.0}}
                    elif operation == failure:
                        code = 'TooManyRequestsException' if operation == request else 'AccessDeniedException'
                        return subprocess.CompletedProcess(arguments, 1, '',
                            f'An error occurred ({code}) when calling the quota operation: '
                            + f'fixture-admin-secret arn:aws:iam::{account_id}:user/private RequestId: {request_id} CaseId: {case_id}')
                    elif failure == 'timeout' and operation == request:
                        raise subprocess.TimeoutExpired('fixture-admin-secret', timeout)
                    elif failure == 'uncoded' and operation == request:
                        return subprocess.CompletedProcess(arguments, 1, '', f'private failure {request_id} {case_id}')
                    elif operation == get_quota:
                        reply = {'Quota': {'Value': quota}}
                    elif operation == history:
                        rows = [{'Status': history_status, 'DesiredValue': 256.0,
                                 'Id': request_id, 'CaseId': case_id, 'Requester': account_id}] if history_status else []
                        if history_status == 'CASE_CLOSED':
                            del rows[0]['DesiredValue']
                        reply = {'RequestedQuotas': rows}
                    else:
                        self.assertEqual(operation, request)
                        reply = {'RequestedQuota': {'Status': request_status, 'DesiredValue': 128.0,
                            'Id': request_id, 'CaseId': case_id, 'Requester': account_id}}
                    return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

                subject.execute = execute
                with (mock.patch.object(deploy, 'Deployment', return_value=subject),
                      mock.patch.object(sys, 'argv', ['deploy.py', 'quota-request', '--settings', str(self.settings)]),
                      redirect_stdout(StringIO()) as captured):
                    self.assertEqual(deploy.main(), 0 if success else 1)
                lines = [line for line in captured.getvalue().splitlines() if 'G and VT' not in line]
                # us-east-2 has only a price read, so it gets no quota call.
                self.assertEqual(gpu_regions, ['us-east-1', 'us-west-2'])
                self.assertEqual([line for line in captured.getvalue().splitlines() if 'G and VT' in line],
                    [f'{region}: G and VT Spot quota 4 vCPUs; no request is needed for 4 vCPUs' for region in gpu_regions])
                self.assertEqual(lines[-1], 'us-west-2: Spot quota 300 vCPUs; no request is needed for 128 vCPUs')
                expected_operations = [get_quota]
                if quota < 128 and failure != get_quota:
                    expected_operations.append(history)
                    if failure != history and history_status not in {'PENDING', 'CASE_OPENED', 'IN_PROGRESS'}:
                        expected_operations.append(request)
                self.assertEqual(calls, [('us-east-1', operation) for operation in expected_operations] + [('us-west-2', get_quota)])
                if failure:
                    reason = {'timeout': 'TimeoutExpired', 'uncoded': 'AwsUnavailable',
                              request: 'TooManyRequestsException'}.get(failure, 'AccessDeniedException')
                    self.assertEqual(lines[0], f'us-east-1: Spot quota request failed: {reason}')
                elif quota >= 128:
                    self.assertEqual(lines[0], f'us-east-1: Spot quota {int(quota)} vCPUs; no request is needed for 128 vCPUs')
                elif history_status in {'PENDING', 'CASE_OPENED', 'IN_PROGRESS'}:
                    self.assertEqual(lines[0], f'us-east-1: open Spot quota request 256 vCPUs; status {history_status}')
                else:
                    self.assertEqual(lines[0], f'us-east-1: Spot quota request 128 vCPUs; status {request_status}')
                for private in ('fixture-admin-id', 'fixture-admin-secret', request_id, case_id, account_id):
                    self.assertNotIn(private, captured.getvalue())

    def test_gpu_quota_request_cases_and_failures_in_each_gate_region(self) -> None:
        get_quota = 'get-service-quota'
        history = 'list-requested-service-quota-change-history-by-quota'
        request = 'request-service-quota-increase'
        candidates = ('us-west-2', 'us-east-2')
        gates = ('us-east-1', *candidates)
        text_setting(self.root / 'cloud_gate.conf', 'regions', ' '.join(gates))
        # eu-north-1 has only a price read, so it gets no quota call.
        text_setting(self.root / 'cloud_gate.conf', 'spot_candidate_regions', ' '.join((*candidates, 'eu-north-1')))
        cases = (
            (8, '', '', '', True),
            (4, '', '', '', True),
            (0, 'PENDING', '', '', True),
            (0, 'CASE_OPENED', '', '', True),
            (0, 'IN_PROGRESS', '', '', True),
            (0, '', 'PENDING', '', True),
            (0, 'APPROVED', 'CASE_OPENED', '', True),
            (0, 'DENIED', 'PENDING', '', True),
            (0, 'CASE_CLOSED', 'PENDING', '', True),
            (0, 'NOT_APPROVED', 'PENDING', '', True),
            (0, 'INVALID_REQUEST', 'PENDING', '', True),
            (0, '', 'DENIED', '', False),
            (0, '', 'NOT_APPROVED', '', False),
            (0, '', 'INVALID_REQUEST', '', False),
            (0, '', '', get_quota, False),
            (0, '', '', history, False),
            (0, '', '', request, False),
            (0, '', '', 'timeout', False),
            (0, '', '', 'uncoded', False),
            (0, '', '', 'malformed', False),
        )
        for selected_region in candidates:
            for quota, history_status, request_status, failure, success in cases:
                with self.subTest(region=selected_region, quota=quota, history=history_status,
                                  request=request_status, failure=failure):
                    subject = self.deployment()
                    calls: list[tuple[str, str, str]] = []

                    def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                                timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                        self.assertTrue(capture_output)
                        self.assertTrue(text)
                        self.assertFalse(check)
                        self.assertNotIn('--profile', arguments)
                        self.assertIsNotNone(env)
                        self.assertEqual(cast(dict[str, str], env)['AWS_ACCESS_KEY_ID'], 'fixture-admin-id')
                        region = arguments[arguments.index('--region') + 1]
                        code = arguments[arguments.index('--quota-code') + 1]
                        operation = arguments[arguments.index('service-quotas') + 1]
                        calls.append((region, code, operation))
                        expected = ['service-quotas', operation, '--service-code', 'ec2', '--quota-code', code]
                        if operation == request:
                            expected.extend(['--desired-value', '4'])
                        self.assertEqual(list(arguments[arguments.index('service-quotas'):]), [*expected, '--output', 'json'])
                        if code == 'L-34B43A08':
                            self.assertIn(region, gates)
                            self.assertEqual(operation, get_quota)
                            reply: dict[str, object] = {'Quota': {'Value': 128}}
                        elif region != selected_region:
                            self.assertEqual(code, 'L-3819A6DF')
                            self.assertEqual(operation, get_quota)
                            reply = {'Quota': {'Value': 4}}
                        elif operation == failure:
                            return subprocess.CompletedProcess(arguments, 1, '',
                                'An error occurred (AccessDeniedException) when calling the quota operation: '
                                + 'private-request private-case private-account fixture-admin-secret')
                        elif operation == request and failure == 'timeout':
                            raise subprocess.TimeoutExpired('private-request', timeout)
                        elif operation == request and failure == 'uncoded':
                            return subprocess.CompletedProcess(arguments, 1, '', 'private-request private-case private-account')
                        elif operation == get_quota:
                            reply = {'Quota': {'Value': quota}}
                        elif operation == history:
                            reply = {'RequestedQuotas': [{'Status': history_status, 'DesiredValue': 8,
                                'Id': 'private-request', 'CaseId': 'private-case', 'Requester': 'private-account'}]
                                if history_status else []}
                        else:
                            self.assertEqual(operation, request)
                            reply = {} if failure == 'malformed' else {'RequestedQuota': {'Status': request_status,
                                'Id': 'private-request', 'CaseId': 'private-case', 'Requester': 'private-account'}}
                        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

                    subject.execute = execute
                    with (mock.patch.object(deploy, 'Deployment', return_value=subject),
                          mock.patch.object(sys, 'argv', ['deploy.py', 'quota-request', '--settings', str(self.settings)]),
                          redirect_stdout(StringIO()) as captured):
                        self.assertEqual(deploy.main(), 0 if success else 1)
                    expected_calls = [(region, 'L-34B43A08', get_quota) for region in gates]
                    for region in gates:
                        expected_calls.append((region, 'L-3819A6DF', get_quota))
                        if region == selected_region and quota < 4 and failure != get_quota:
                            expected_calls.append((region, 'L-3819A6DF', history))
                            if failure != history and history_status not in {'PENDING', 'CASE_OPENED', 'IN_PROGRESS'}:
                                expected_calls.append((region, 'L-3819A6DF', request))
                    self.assertEqual(calls, expected_calls)
                    lines = captured.getvalue().splitlines()
                    standard = [f'{region}: Spot quota 128 vCPUs; no request is needed for 128 vCPUs' for region in gates]
                    self.assertEqual(lines[:len(gates)], standard)
                    if failure:
                        reason = {'timeout': 'TimeoutExpired', 'uncoded': 'AwsUnavailable', 'malformed': 'KeyError'}.get(
                            failure, 'AccessDeniedException')
                        expected_line = f'{selected_region}: G and VT Spot quota request failed: {reason}'
                    elif quota >= 4:
                        expected_line = f'{selected_region}: G and VT Spot quota {quota} vCPUs; no request is needed for 4 vCPUs'
                    elif history_status in {'PENDING', 'CASE_OPENED', 'IN_PROGRESS'}:
                        expected_line = f'{selected_region}: open G and VT Spot quota request 8 vCPUs; status {history_status}'
                    else:
                        expected_line = f'{selected_region}: G and VT Spot quota request 4 vCPUs; status {request_status}'
                    self.assertEqual(lines, [*standard, *[
                        expected_line if region == selected_region else
                        f'{region}: G and VT Spot quota 4 vCPUs; no request is needed for 4 vCPUs' for region in gates]])
                    for private in ('private-request', 'private-case', 'private-account', 'fixture-admin-id', 'fixture-admin-secret'):
                        self.assertNotIn(private, captured.getvalue())

    def test_regional_gate_stack_reuses_home_resources_and_copies_missing_image(self) -> None:
        subject, commands = self.gate_fixture()
        home_api = subject._api(aws_config.read_deployment_settings(self.settings))  # pyright: ignore[reportPrivateUsage]
        original = home_api.execute
        copied = False
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            region = args[args.index('--region') + 1]
            operation = args[args.index('--region') + 3]
            reply: dict[str, object] = {}
            nonlocal copied
            if operation == 'describe-vpcs':
                reply = {'Vpcs': [{'VpcId': 'vpc-fixture-west'}]}
            elif operation == 'describe-images':
                reply = {'Images': [{'ImageId': 'ami-fixture-home', 'Name': 'fixture-nixos', 'OwnerId': 'fixture-owner'}]
                         if region == 'us-east-2' else []}
            elif operation == 'copy-image':
                copied = True
                reply = {'ImageId': 'ami-fixture-west'}
            elif operation == 'describe-stacks' and region == 'us-west-2':
                reply = {'Stacks': [{'StackStatus': 'CREATE_COMPLETE', 'StackId': 'fixture-stack-west',
                    'Outputs': [{'OutputKey': key, 'OutputValue': value} for key, value in
                    {'GateLaunchTemplate': 'lt-west', 'GateLaunchVersion': '2', 'GateSecurityGroup': 'sg-west'}.items()]}]}
            elif operation not in ('deploy', 'wait'):
                return original(arguments, capture_output=True, text=True, timeout=1, check=False)
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')
        api = aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute)
        settings = aws_config.read_deployment_settings(self.settings)
        home = {'GateProfileArn': 'fixture-profile', 'GateRoleArn': 'fixture-role'}
        digests = deploy.RegionalStackDigests.read(subject.directory)
        result = subject.regional_gate_stack(api, settings, home, {}, subject.regional_network(api, settings, 'us-west-2'), digests)
        self.assertTrue(copied)
        self.assertEqual(result['image'], 'ami-fixture-west')
        self.assertEqual(result['region'], 'us-west-2')
        self.assertEqual(subject.regional_gate_stack(api, settings, home, {},
            subject.regional_network(api, settings, 'us-west-2'), digests), result)
        self.assertEqual(sum('copy-image' in command for command in commands), 1)
        self.assertEqual(sum('deploy' in command and settings.stack_name + '-gates-us-west-2' in command for command in commands), 1)
        query = next(command for command in commands if 'describe-images' in command and '--owners' in command)
        self.assertIn('fixture-owner', query)
        self.assertIn('Name=name,Values=fixture-nixos', query)
        resources = ledger.mapping(deploy.region_gate_template()['Resources'])
        self.assertEqual(len(resources), 7)
        self.assertFalse(any(ledger.text(ledger.mapping(row), 'Type').startswith('AWS::IAM::') and name != 'GateExpiryRole'
                             for name, row in resources.items()))
        self.assertNotIn('GateBucket', resources)
        self.assertNotIn('GateProfile', resources)
        gate = conf.read_or_repository('cloud_gate')
        regions = {*gate.words('regions'), *gate.words('spot_candidate_regions')}
        limits = (('GateSecurityGroup', 'GroupName', 255), ('GateLaunchTemplate', 'LaunchTemplateName', 128),
                  ('GateExpiryRole', 'RoleName', 64), ('GateExpiryFunction', 'FunctionName', 64),
                  ('GateExpiryLog', 'LogGroupName', 512), ('GateExpirySchedule', 'Name', 64))
        for name, key, limit in limits:
            properties = ledger.mapping(ledger.mapping(resources[name])['Properties'])
            value = ledger.text(ledger.mapping(properties[key]), 'Fn::Sub')
            for region in regions:
                with self.subTest(name=name, region=region):
                    named = value.replace('${DeploymentId}', 'f' * 32).replace('${AWS::Region}', region)
                    self.assertLessEqual(len(named), limit)

    def test_concurrent_regional_deploys_read_private_templates(self) -> None:
        first, _commands = self.gate_fixture()
        other_state = self.root / 'other-state'
        other_state.mkdir()
        _ = (other_state / 'deployment.json').write_bytes((first.directory / 'deployment.json').read_bytes())
        settings = aws_config.read_deployment_settings(self.settings)
        home = {'GateProfileArn': 'fixture-profile'}
        network = deploy.RegionalNetwork('us-west-2', 'fixture-vpc', [], 'fixture-image')
        definition = deploy.region_gate_template()
        templates = ({**definition, 'Description': 'first template'}, {**definition, 'Description': 'second template'})
        paths: list[Path] = []
        outputs = {'GateLaunchTemplate': 'lt-west', 'GateLaunchVersion': '1', 'GateSecurityGroup': 'sg-west'}

        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            if 'describe-stacks' in args:
                reply = {'Stacks': [{'StackStatus': 'CREATE_COMPLETE', 'StackId': 'fixture-stack',
                    'Outputs': [{'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()]}]}
                return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')
            self.assertIn('deploy', args)
            path = Path(args[args.index('--template-file') + 1])
            index = len(paths)
            paths.append(path)
            if index == 0:
                # Start the second deploy before the first CLI reads its template.
                _ = second.regional_gate_stack(api, settings, home, {}, network,
                    deploy.RegionalStackDigests.read(second.directory))
            self.assertEqual(cast(object, json.loads(path.read_text())), templates[index])
            return subprocess.CompletedProcess(args, 0, '', '')

        second = deploy.Deployment(self.settings, other_state, first.output, execute=execute, clock=first.clock)
        api = aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute)
        with mock.patch.object(deploy, 'region_gate_template', side_effect=templates):
            _ = first.regional_gate_stack(api, settings, home, {}, network,
                deploy.RegionalStackDigests.read(first.directory))
        self.assertEqual(len(set(paths)), 2)
        for path in paths:
            self.assertFalse(path.parent.exists())

    def regional_stack_fixture(self, stack: dict[str, str]) -> tuple[deploy.Deployment, list[list[str]], aws_target.AwsCli]:
        """A regional stack in the state stack['status']; an empty status means the stack does not exist yet.

        stack['deploy'] is 'fails' or 'passes', and the fixture sets it to 'done' after a passing deploy."""
        subject, commands = self.gate_fixture()
        original = subject._api(aws_config.read_deployment_settings(self.settings)).execute  # pyright: ignore[reportPrivateUsage]
        def stamp(at: int) -> str:
            return datetime.fromtimestamp(at, UTC).isoformat()
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            region = args[args.index('--region') + 1]
            operation = args[args.index('--region') + 3]
            reply: dict[str, object] = {}
            if operation == 'describe-vpcs':
                reply = {'Vpcs': [{'VpcId': 'vpc-fixture-west'}]}
            elif operation == 'describe-images':
                reply = {'Images': [{'ImageId': 'ami-fixture', 'Name': 'fixture-nixos', 'OwnerId': 'fixture-owner'}]}
            elif operation == 'describe-stacks' and region == 'us-west-1' and stack['deploy'] != 'done':
                if not stack['status']:
                    return subprocess.CompletedProcess(args, 255, '', 'An error occurred (ValidationError): Stack with id x does not exist')
                reply = {'Stacks': [{'StackStatus': stack['status']}]}
            elif operation == 'describe-stacks' and region == 'us-west-1':
                reply = {'Stacks': [{'StackStatus': 'CREATE_COMPLETE', 'Outputs': [
                    {'OutputKey': key, 'OutputValue': value} for key, value in
                    {'GateLaunchTemplate': 'lt-west', 'GateLaunchVersion': '1', 'GateSecurityGroup': 'sg-west'}.items()]}]}
            elif operation == 'deploy':
                commands.append(args)
                if stack['deploy'] == 'fails':
                    return subprocess.CompletedProcess(args, 255, '', 'aws: [ERROR]: Failed to create/update the stack.')
                stack['deploy'] = 'done'
                return subprocess.CompletedProcess(args, 0, '', '')
            elif operation == 'describe-stack-events':
                reply = {'StackEvents': [
                    {'Timestamp': stamp(self.now), 'LogicalResourceId': 'GateExpiryFunction',
                     'ResourceType': 'AWS::Lambda::Function', 'ResourceStatus': 'CREATE_FAILED',
                     'ResourceStatusReason': 'fixture runtime is not offered here'},
                    {'Timestamp': stamp(self.now - 600), 'LogicalResourceId': 'GateSecurityGroup',
                     'ResourceType': 'AWS::EC2::SecurityGroup', 'ResourceStatus': 'CREATE_FAILED',
                     'ResourceStatusReason': 'fixture failure of an earlier run'}]}
            elif operation not in ('delete-stack', 'wait'):
                return original(arguments, capture_output=True, text=True, timeout=1, check=False)
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')
        return subject, commands, aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute)

    def test_regional_stack_failure_names_the_region_and_the_failed_resources_of_this_run(self) -> None:
        subject, commands, api = self.regional_stack_fixture({'status': '', 'deploy': 'fails'})
        settings = aws_config.read_deployment_settings(self.settings)
        path = self.state / 'regional-stack-digests.json'
        prior = {'digest': 'a' * 64, 'StackId': 'fixture-stack', 'LastUpdatedTime': None}
        write_text_atomic(path, json.dumps({'us-west-1': prior, 'us-west-2': prior}))
        with self.assertRaises(aws_target.AwsUnavailable) as raised:
            _ = subject.regional_gate_stack(api, settings, {'GateProfileArn': 'p', 'GateRoleArn': 'r'}, {},
                subject.regional_network(api, settings, 'us-west-1'), deploy.RegionalStackDigests.read(self.state))
        lines = str(raised.exception).splitlines()
        self.assertEqual(lines[0], 'us-west-1: aws: [ERROR]: Failed to create/update the stack.')
        self.assertEqual(lines[1:], ['GateExpiryFunction (AWS::Lambda::Function): CREATE_FAILED: fixture runtime is not offered here'])
        self.assertEqual(remote.read_record(path), {'us-west-2': prior})
        command = next(command for command in commands if 'deploy' in command)
        self.assertFalse(Path(command[command.index('--template-file') + 1]).parent.exists())

    def test_regional_stack_whose_first_create_failed_is_removed_before_the_new_deploy(self) -> None:
        stack = {'status': '', 'deploy': 'passes'}
        subject, commands, api = self.regional_stack_fixture(stack)
        settings = aws_config.read_deployment_settings(self.settings)
        for status, removed in (('ROLLBACK_COMPLETE', True), ('', False)):
            with self.subTest(status=status or 'no stack'):
                stack.update(status=status, deploy='passes')
                commands.clear()
                with redirect_stdout(StringIO()) as output:
                    result = subject.regional_gate_stack(api, settings, {'GateProfileArn': 'p', 'GateRoleArn': 'r'}, {},
                        subject.regional_network(api, settings, 'us-west-1'), deploy.RegionalStackDigests.read(self.state))
                self.assertEqual(result['template'], 'lt-west')
                operations = [command[command.index('--region') + 3] for command in commands
                              if command[command.index('--region') + 1] == 'us-west-1'
                              and command[command.index('--region') + 3] in ('delete-stack', 'wait', 'deploy')]
                self.assertEqual(operations, ['delete-stack', 'wait', 'deploy'] if removed else ['deploy'])
                self.assertEqual('removing the gate stack whose first create failed' in output.getvalue(), removed)

    def scaler_policy_count(self, regions: int) -> int:
        """The managed policies attached to the scaler user when gates run in this many regions."""
        home = ledger.mapping(ledger.mapping(cast(object, json.loads(deploy.TEMPLATE.read_text())))['Resources'])
        def users(row: object) -> list[object]:
            return list(cast(list[object], ledger.mapping(row).get('Users', [])))
        attached = [name for name, row in home.items() if ledger.text(ledger.mapping(row), 'Type') == 'AWS::IAM::ManagedPolicy'
                    and {'Ref': 'ScalerUser'} in users(ledger.mapping(row)['Properties'])]
        self.assertEqual(sorted(attached), ['GateGpuTrialScalerPolicy', 'GateScalerPolicy'])
        def block_users(row: object) -> list[object]:
            definition = ledger.mapping(ledger.mapping(ledger.mapping(row)['Properties'])['Definition'])
            return users(definition.get('IamActionDefinition', {}))
        blocks = [row for row in home.values() if ledger.text(ledger.mapping(row), 'Type') == 'AWS::Budgets::BudgetsAction'
                  and {'Ref': 'ScalerUser'} in block_users(row)]
        regional = [row for row in ledger.mapping(deploy.region_gate_template()['Resources']).values()
                    if ledger.text(ledger.mapping(row), 'Type') in ('AWS::IAM::ManagedPolicy', 'AWS::IAM::Policy')]
        return len(attached) + len(blocks) + len(regional) * (regions - 1)

    def test_scaler_user_keeps_two_gate_policies_and_the_budget_block_in_every_region_count(self) -> None:
        gates = conf.read(self.root / 'cloud_gate.conf')
        for key in ('regions', 'spot_candidate_regions'):
            with self.subTest(regions=len(gates.words(key))):
                # AWS attaches at most 10 managed policies to one IAM user; the budget block needs a free slot.
                self.assertEqual(self.scaler_policy_count(len(gates.words(key))), 3)

    def test_gate_scaler_policy_for_every_candidate_region_fits_the_managed_policy_size(self) -> None:
        gates = conf.read(self.root / 'cloud_gate.conf')
        regions = gates.words('spot_candidate_regions')
        partition, account = 'aws', '1' * 12
        values: dict[str, object] = {
            'GateVpcs': [f'arn:{partition}:ec2:{region}:{account}:vpc/vpc-{"f" * 17}' for region in regions],
            'GateRegionImages': [f'arn:{partition}:ec2:{region}::image/ami-{"f" * 17}' for region in regions[1:]],
            'GateInstanceTypes': gates.words('instance_types'), 'GateGpuInstanceTypes': gates.words('gpu_instance_types')}
        names = {'AWS::Partition': partition, 'AWS::AccountId': account, 'AWS::Region': regions[0]}
        def render(value: object) -> object:
            if isinstance(value, str):
                return re.sub(r'\$\{([^}]+)\}', lambda found: names.get(found.group(1), 'x' * 64), value)
            if isinstance(value, list):
                return [render(item) for item in cast(list[object], value)]
            if isinstance(value, dict):
                row = cast(dict[str, object], value)
                if 'Ref' in row:
                    return values.get(ledger.text(row, 'Ref'), 'x' * 64)
                if 'Fn::Sub' in row or 'Fn::GetAtt' in row:
                    return render(row.get('Fn::Sub', 'x' * 128))
                if 'Fn::If' in row:
                    return render(cast(list[object], row['Fn::If'])[1])
                return {key: render(item) for key, item in row.items()}
            return value
        home = ledger.mapping(ledger.mapping(cast(object, json.loads(deploy.TEMPLATE.read_text())))['Resources'])
        for name in ('GateScalerPolicy', 'GateGpuTrialScalerPolicy'):
            with self.subTest(policy=name):
                document = render(ledger.mapping(ledger.mapping(home[name])['Properties'])['PolicyDocument'])
                # AWS counts a managed policy document without white space, to at most 6144 characters.
                self.assertLessEqual(len(json.dumps(document, separators=(',', ':'))), 6144)

    def test_regional_image_uses_matching_name_without_copy(self) -> None:
        subject = self.deployment()
        calls: list[list[str]] = []
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            calls.append(args)
            home = args[args.index('--region') + 1] == 'us-east-2'
            image = {'ImageId': 'ami-home' if home else 'ami-west', 'Name': 'fixture-nixos', 'OwnerId': 'fixture-owner'}
            return subprocess.CompletedProcess(args, 0, json.dumps({'Images': [image]}), '')
        api = aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute)
        self.assertEqual(subject.regional_image(api, aws_config.read_deployment_settings(self.settings), 'us-west-2'), 'ami-west')
        self.assertFalse(any('copy-image' in command for command in calls))

    def test_regional_network_refuses_missing_default_vpc(self) -> None:
        subject = self.deployment()
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(arguments, 0, '{"Vpcs": []}', '')
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'us-west-2: no default VPC'):
            _ = subject.regional_network(aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute),
                aws_config.read_deployment_settings(self.settings), 'us-west-2')

    def test_regional_network_refuses_no_public_subnet(self) -> None:
        subject = self.deployment()
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            reply: dict[str, object] = {'Vpcs': [{'VpcId': 'vpc-fixture'}]}
            if 'describe-route-tables' in args:
                reply = {'RouteTables': []}
            elif 'describe-subnets' in args:
                reply = {'Subnets': [{'SubnetId': 'subnet-fixture', 'AvailabilityZone': 'us-west-2a', 'State': 'available'}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'no available public subnet'):
            _ = subject.regional_network(aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute),
                aws_config.read_deployment_settings(self.settings), 'us-west-2')

    def test_regional_image_cleanup_recovers_after_deregistration_record_failure(self) -> None:
        from scripts.aws_target import teardown
        path = self.state / 'regional-images.json'
        _ = path.write_text(json.dumps({'copy': {'region': 'us-west-2', 'image': 'ami-west'}}))
        deleted = False
        snapshots: list[str] = []
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            nonlocal deleted
            args = list(arguments)
            reply: dict[str, object] = {}
            error = ''
            if 'describe-images' in args:
                reply = {'Images': [{'BlockDeviceMappings': [{'Ebs': {'SnapshotId': 'snap-west'}}]}]}
            elif 'deregister-image' in args:
                if deleted:
                    error = 'InvalidAMIID.NotFound'
                deleted = True
            elif 'delete-snapshot' in args:
                snapshots.append(args[args.index('--snapshot-id') + 1])
            return subprocess.CompletedProcess(args, int(bool(error)), json.dumps(reply), error)
        api = aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute)
        write = write_text_atomic
        def fail_record(destination: Path, text: str) -> None:
            if 'deregistered' in text:
                raise OSError('fixture record failure')
            write(destination, text)
        with mock.patch.object(teardown, 'write_text_atomic', side_effect=fail_record):
            with self.assertRaisesRegex(OSError, 'fixture record failure'):
                _ = teardown.remove_regional_gates(api, self.state)
        self.assertTrue(deleted)
        self.assertTrue(teardown.remove_regional_gates(api, self.state))
        self.assertEqual(snapshots, ['snap-west'])
        self.assertFalse(teardown.remove_regional_gates(api, self.state))

    def test_down_removes_regional_stack_and_copied_image_before_snapshot(self) -> None:
        from scripts.aws_target import teardown
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2 us-west-2')
        cloud_gate_record = {'deployment_id': 'fixture', 'regions': [{'region': 'us-east-2'},
            {'region': 'us-west-2', 'stack': 'fixture-gates-us-west-2'}]}
        _ = (self.state / 'gate-deployment.json').write_text(json.dumps(cloud_gate_record))
        _ = (self.state / 'regional-images.json').write_text(json.dumps({'copy': {'region': 'us-west-2', 'image': 'ami-west'}}))
        calls: list[list[str]] = []
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            calls.append(args)
            reply: dict[str, object] = {'Reservations': []}
            if 'describe-instances' in args and args[args.index('--region') + 1] == 'us-west-2':
                reply = {'Reservations': [{'Instances': [{'InstanceId': 'i-fixture-west'}]}]}
            if 'describe-images' in args:
                reply = {'Images': [{'BlockDeviceMappings': [{'Ebs': {'SnapshotId': 'snap-west'}}]}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')
        api = aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute)
        self.assertTrue(teardown.remove_regional_gates(api, self.state))
        operations = [command[command.index('--region') + 3] for command in calls]
        self.assertLess(operations.index('terminate-instances'), operations.index('wait'))
        self.assertLess(operations.index('wait'), operations.index('delete-stack'))
        self.assertLess(operations.index('delete-stack'), operations.index('deregister-image'))
        self.assertLess(operations.index('deregister-image'), operations.index('delete-snapshot'))
        sweeps = [command for command in calls if 'describe-instances' in command]
        self.assertEqual([command[command.index('--region') + 1] for command in sweeps], ['us-east-2', 'us-west-2'])
        self.assertTrue(all(command[command.index('--region') + 1] == 'us-west-2' for command in calls if command not in sweeps))
        self.assertFalse(teardown.remove_regional_gates(api, self.state))


    def test_gate_stack_updates_print_named_start_and_done_lines(self) -> None:
        image_titles = ('switching gates to the official image', 'removing the old image permission')
        for command, titles in (('up', ('updating the stack',)), ('remove-images', image_titles)):
            with self.subTest(command=command):
                if command == 'up':
                    subject, _ = self.gate_fixture()
                    with redirect_stdout(StringIO()) as output:
                        self.assertTrue(subject.gate_up(approve_resources=True))
                else:
                    subject, _ = self.gate_image_fixture()
                    with redirect_stdout(StringIO()) as output:
                        self.assertTrue(subject.remove_images(approve_destroy=True))
                lines = output.getvalue().splitlines()
                for title in titles:
                    progress = [line for line in lines if line.startswith(f'{command}: {title}')]
                    self.assertEqual(len(progress), 2)
                    self.assertEqual(progress[0], f'{command}: {title} (can take several minutes)')
                    self.assertRegex(progress[1], '^' + re.escape(f'{command}: {title}') + r' done in \d+ s$')

    def test_gate_deployment_preserves_rust_parameters_and_records_scoped_resources(self) -> None:
        subject, commands = self.gate_fixture()
        before = ledger.CostLedger(self.state).read_snapshot().inventories[-1].resources
        self.assertTrue(subject.gate_up(approve_resources=True))
        arguments = next(args for args in commands if 'create-change-set' in args)
        parameters = ledger.rows({'rows': cast(object, json.loads(arguments[arguments.index('--parameters') + 1]))}, 'rows')
        by_name = {ledger.text(row, 'ParameterKey'): row for row in parameters}
        for key in ('VpcId', 'NotificationEmail'):
            self.assertEqual(by_name[key], {'ParameterKey': key, 'UsePreviousValue': True})
        self.assertEqual(by_name['GateInstanceTypes']['ParameterValue'], 'm7a.8xlarge,m7a.4xlarge')
        self.assertEqual(by_name['GateRootGiB']['ParameterValue'], '40')
        self.assertEqual(by_name['GateSpotMaxPrice']['ParameterValue'], '0.8')
        saved = remote.read_record(self.state / 'gate-deployment.json')
        self.assertEqual(saved['artifact_prefix'], 'artifacts/fixture-role:')
        self.assertEqual(saved['revision'], 'fixture-revision')
        self.assertEqual([row['zone'] for row in ledger.rows(ledger.rows(saved, 'regions')[0], 'subnets')], ['a', 'b'])
        after = ledger.CostLedger(self.state).read_snapshot().inventories[-1].resources
        self.assertEqual(after, before + (ledger.PricedResource('gate-shared', Decimal(1)),))
        self.assertTrue(any('execute-change-set' in args for args in commands))

    def test_gate_setup_grants_and_records_the_sorted_union_of_gpu_trial_types(self) -> None:
        subject, commands = self.gate_fixture()
        self.configure_gate(
            'regions=us-east-2\nhana_gpu_instance_types=g6.2xlarge g4dn.2xlarge\n' +
            'gpu_size_trial_spot_max_usd_per_hour={"g6.2xlarge":0.98,"g4dn.xlarge":0.5}')
        self.assertTrue(subject.gate_up(approve_resources=True))
        arguments = next(args for args in commands if 'create-change-set' in args)
        parameters = ledger.rows({'rows': cast(object, json.loads(arguments[arguments.index('--parameters') + 1]))}, 'rows')
        applied = next(row['ParameterValue'] for row in parameters if row['ParameterKey'] == 'GateGpuInstanceTypes')
        expected = ['g4dn.2xlarge', 'g4dn.xlarge', 'g6.2xlarge']
        self.assertEqual(applied, ','.join(expected))
        self.assertEqual(remote.read_record(self.state / 'gate-deployment.json')['gpu_instance_types'], expected)

    def test_interrupted_gate_setup_removes_the_previous_pass_before_regional_preparation(self) -> None:
        subject, _commands = self.gate_fixture()
        with redirect_stdout(StringIO()):
            self.assertTrue(subject.gate_up(approve_resources=True))
        self.assertIsInstance(gate_probe.read_record(self.state), gate_probe.GateProbeRecord)
        self.configure_gate('regions=us-east-2 us-west-2')

        def interrupt(*_args: object) -> deploy.RegionalNetwork:
            self.assertFalse((self.state / gate_probe.FILE_NAME).exists())
            raise KeyboardInterrupt

        with (mock.patch.object(subject, 'regional_network', side_effect=interrupt),
              redirect_stdout(StringIO()), self.assertRaises(KeyboardInterrupt)):
            _ = subject.gate_up(approve_resources=True)
        self.assertIs(gate_probe.read_record(self.state), gate_probe.ProbeRecordUnavailable.ABSENT)

    def test_gate_setup_proves_each_saved_gpu_type_and_the_result_transfer(self) -> None:
        subject, commands = self.gate_fixture()
        self.configure_gate(
            'regions=us-east-2\nhana_gpu_instance_types=g4dn.2xlarge\n' +
            'gpu_size_trial_spot_max_usd_per_hour={"g6.2xlarge":0.98}')
        subject.clock = lambda: self.now + 3 if any('abort-multipart-upload' in args for args in commands) else self.now
        with (mock.patch.dict(os.environ, {'AWS_ACCESS_KEY_ID': 'fixture-admin', 'AWS_PROFILE': 'build-admin'}),
              redirect_stdout(StringIO()) as output):
            self.assertTrue(subject.gate_up(approve_resources=True))

        reports = [line.removeprefix('gate-setup probe: ') for line in output.getvalue().splitlines()
                   if line.startswith('gate-setup probe: ')]
        self.assertEqual(reports, [
            'get-caller-identity: pass (OK)', 'describe-instances: pass (OK)', 'describe-instance-type-offerings: pass (OK)',
            'describe-spot-price-history: pass (OK)', 'get-service-quota: pass (OK)', 'get-cost-and-usage: pass (OK)',
            'run-instances g4dn.2xlarge: pass (DryRunOperation)', 'run-instances g6.2xlarge: pass (DryRunOperation)',
            'terminate-instances: pass (DryRunOperation)', 'termination us-east-2: pass (IAM simulation)',
            'export-credentials: pass (OK)', 'put-object: pass (OK)',
            'list-objects-v2: pass (OK)', 'get-object: pass (OK)', 'put-object inputs: pass (OK)',
            'get-object inputs: pass (OK)',
            'create/abort-multipart-upload: pass (OK)',
        ])
        self.assertLess(output.getvalue().index('cloud-gate: deployed'), output.getvalue().index('gate-setup probe: '))
        saved = remote.read_record(self.state / 'gate-deployment.json')
        recorded = gate_probe.read_record(self.state)
        assert isinstance(recorded, gate_probe.GateProbeRecord)
        self.assertEqual(recorded.deployment_id, saved['deployment_id'])
        self.assertEqual(recorded.ended_at, datetime.fromtimestamp(self.now + 3, UTC))
        self.assertEqual([check.report() for check in recorded.checks], reports)
        self.assertEqual([row for row in recorded.checks if isinstance(row.check, gate_probe.GpuLaunchCheck)], [
            gate_probe.ProbeCheck(gate_probe.GpuLaunchCheck('us-east-2', 'g4dn.2xlarge'),
                                  gate_probe.ProbeVerdict.PASS, gate_probe.ProbeReason.DRY_RUN),
            gate_probe.ProbeCheck(gate_probe.GpuLaunchCheck('us-east-2', 'g6.2xlarge'),
                                  gate_probe.ProbeVerdict.PASS, gate_probe.ProbeReason.DRY_RUN),
        ])
        self.assertEqual((self.state / gate_probe.FILE_NAME).stat().st_mode & 0o777, 0o600)
        launches = self.gpu_probe_launches(commands)
        self.assertEqual([launch['InstanceType'] for launch in launches], saved['gpu_instance_types'])
        for launch in launches:
            self.assertEqual(launch['LaunchTemplate'], {'LaunchTemplateId': 'fixture-template', 'Version': '1'})
            self.assertEqual(launch['ImageId'], 'ami-fixture')
            self.assertEqual((launch['MinCount'], launch['MaxCount']), (1, 1))
            subnet = 'fixture-b' if launch['InstanceType'] == 'g4dn.2xlarge' else 'fixture-a'
            self.assertEqual(launch['NetworkInterfaces'], [{'DeviceIndex': 0, 'SubnetId': subnet,
                                                           'Groups': ['fixture-group'], 'AssociatePublicIpAddress': True}])
            market = ledger.mapping(launch['InstanceMarketOptions'])
            self.assertEqual(market['MarketType'], 'spot')
            options = ledger.mapping(market['SpotOptions'])
            self.assertEqual((options['SpotInstanceType'], options['InstanceInterruptionBehavior']), ('one-time', 'terminate'))
            specs = ledger.rows(launch, 'TagSpecifications')
            self.assertEqual([spec['ResourceType'] for spec in specs], ['instance', 'volume'])
            for spec in specs:
                tags = {ledger.text(tag, 'Key'): tag['Value'] for tag in ledger.rows(spec, 'Tags')}
                self.assertEqual(tags['GateDeployment'], saved['deployment_id'])
                self.assertEqual(tags['Workload'], 'cloud-claude-gate')
                self.assertEqual(tags['GateAttempt'], launch['ClientToken'])
                self.assertEqual(tags['Project'], 'fixture-build')
                self.assertTrue(tags['GpuSizeTrial'])
                self.assertGreater(int(str(tags['ExpiresAt'])), self.now)
        self.assertNotIn('create-tags', [args[6] for args in commands if args[:1] == ['aws'] and '--profile' in args])

    @staticmethod
    def gpu_probe_launches(commands: list[list[str]]) -> list[dict[str, object]]:
        return [ledger.mapping(cast(object, json.loads(args[args.index('--cli-input-json') + 1])))
                for args in commands if 'run-instances' in args]

    def failed_gpu_probe_output(self, operation: str, code: str, *,
                                fault: str = '', advice: str = 'Check the gate scaler permissions and repeat approved setup.') -> str:
        subject, _commands = self.gate_fixture(probe_faults={fault or operation: code})
        argv = ['deploy.py', 'up', '--workload', 'merge-gates', '--approve-resources']
        with (mock.patch.object(deploy, 'Deployment', return_value=subject), mock.patch.object(sys, 'argv', argv),
              redirect_stdout(StringIO()) as output):
            self.assertEqual(deploy.main(), 1)
        result = output.getvalue()
        self.assertIn(f'gate-setup probe: {operation}: fail ({code})', result)
        self.assertNotIn('private diagnostic', result)
        self.assertEqual(result.splitlines()[-1], f'gate-setup: {advice}')
        return result

    def test_gate_setup_fails_on_one_refused_gpu_type_and_finishes_the_other_probes(self) -> None:
        path = self.state / gate_probe.FILE_NAME
        _ = path.write_text('previous record')
        path.chmod(0o644)
        output = self.failed_gpu_probe_output('run-instances g4dn.2xlarge', 'UnauthorizedOperation')
        self.assertIn('gate-setup probe: run-instances g6.2xlarge: pass (DryRunOperation)', output)
        self.assertIn('gate-setup probe: get-object: pass (OK)', output)
        recorded = gate_probe.read_record(self.state)
        assert isinstance(recorded, gate_probe.GateProbeRecord)
        failures = [check for check in recorded.checks if check.verdict is gate_probe.ProbeVerdict.FAIL]
        self.assertEqual(failures, [
            gate_probe.ProbeCheck(gate_probe.GpuLaunchCheck('us-east-2', 'g4dn.2xlarge'),
                                  gate_probe.ProbeVerdict.FAIL, gate_probe.ProbeReason.CHECK_FAILED),
        ])
        self.assertIs(recorded.checks[-1].check.name, gate_probe.RegionalCheckName.MULTIPART_INPUT)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        raw = path.read_text()
        for private in ('previous record', 'UnauthorizedOperation', 'private diagnostic', 'https://', 'arn:',
                        'fixture-account', 'fixture-bucket', 'fixture-template', 'fixture-group',
                        'fixture-upload', 'ami-fixture', 'i-fixture'):
            with self.subTest(private=private):
                self.assertNotIn(private, raw)

    def test_gate_setup_fails_on_an_s3_403(self) -> None:
        output = self.failed_gpu_probe_output('list-objects-v2', '403')
        self.assertIn('gate-setup probe: get-object: pass (OK)', output)

    def test_gate_setup_fails_on_cost_explorer_denial(self) -> None:
        output = self.failed_gpu_probe_output('get-cost-and-usage', 'AccessDeniedException')
        self.assertIn('gate-setup probe: get-object inputs: pass (OK)', output)

    def test_gate_setup_fails_on_input_get_denial(self) -> None:
        _ = self.failed_gpu_probe_output('get-object inputs', 'AccessDenied')

    def test_gate_setup_fails_on_multipart_abort_denial(self) -> None:
        _ = self.failed_gpu_probe_output('create/abort-multipart-upload', 'AccessDenied', fault='abort-multipart-upload')

    def test_gate_setup_fails_when_scaler_credentials_do_not_export(self) -> None:
        _ = self.failed_gpu_probe_output('export-credentials', 'AccessDenied')

    def test_gate_setup_lists_only_the_exact_result_probe_key(self) -> None:
        subject, commands = self.gate_fixture()
        with redirect_stdout(StringIO()) as output:
            self.assertTrue(subject.gate_up(approve_resources=True))
        listed_keys = [args[args.index('--prefix') + 1] for args in commands if 'list-objects-v2' in args]
        self.assertEqual(listed_keys, ['artifacts/gpu-size-trial/setup-probe.json'])
        self.assertIn('list-objects-v2: pass (OK)', output.getvalue())
        self.assertIn('get-object inputs: pass (OK)', output.getvalue())

    def test_gate_setup_skips_dry_run_without_an_offered_subnet(self) -> None:
        size = 'g4dn.2xlarge'
        subject, commands = self.gate_fixture(unoffered_gpu_type=size)
        with redirect_stdout(StringIO()) as output:
            self.assertTrue(subject.gate_up(approve_resources=True))
        self.assertIn(f'run-instances {size} unverified. No saved subnet offers this type.', output.getvalue())
        launches = self.gpu_probe_launches(commands)
        self.assertTrue(launches)
        self.assertTrue(all(launch['InstanceType'] != size for launch in launches))
        recorded = gate_probe.read_record(self.state)
        assert isinstance(recorded, gate_probe.GateProbeRecord)
        self.assertIn(gate_probe.ProbeCheck(gate_probe.GpuLaunchCheck('us-east-2', size),
                     gate_probe.ProbeVerdict.UNVERIFIED, gate_probe.ProbeReason.NO_OFFERED_SUBNET), recorded.checks)

    def test_gate_setup_retries_denied_launch_within_the_configured_deadline(self) -> None:
        self.check_gpu_launch_retries(('UnauthorizedOperation', 'DryRunOperation'), [2], success=True)

    def test_gate_setup_stops_retrying_denied_launch_at_the_configured_deadline(self) -> None:
        self.check_gpu_launch_retries(('UnauthorizedOperation',) * 3, [2, 2], success=False)

    def check_gpu_launch_retries(self, codes: tuple[str, ...], expected_waits: list[float], *, success: bool) -> None:
        operation = 'run-instances g4dn.2xlarge'
        subject, commands = self.gate_fixture(probe_faults={operation: codes})
        self.configure_gate(
            'gpu_setup_dry_run_retry_timeout_s=5\ngpu_setup_dry_run_retry_interval_s=2')
        clock = 0.0
        waits: list[float] = []

        def pause(seconds: float) -> None:
            nonlocal clock
            clock += seconds
            waits.append(seconds)

        subject.pause = pause
        with mock.patch.object(time, 'monotonic', side_effect=lambda: clock), redirect_stdout(StringIO()) as output:
            self.assertEqual(subject.gate_up(approve_resources=True), success)
        self.assertEqual(waits, expected_waits)
        self.assertEqual(output.getvalue().count(f'{operation}: UnauthorizedOperation. Retry in 2 s.'), len(waits))
        launches = self.gpu_probe_launches(commands)
        self.assertEqual(sum(launch['InstanceType'] == 'g4dn.2xlarge' for launch in launches), len(waits) + 1)

    def test_gate_setup_gives_retry_advice_for_a_timeout(self) -> None:
        _ = self.failed_gpu_probe_output('list-objects-v2', 'TimeoutExpired', advice='Run approved setup again.')

    def test_gate_setup_gives_retry_advice_for_throttling(self) -> None:
        _ = self.failed_gpu_probe_output('list-objects-v2', 'ThrottlingException', advice='Run approved setup again.')

    def test_gate_setup_gives_retry_advice_for_cost_explorer_throttling(self) -> None:
        _ = self.failed_gpu_probe_output('get-cost-and-usage', 'LimitExceededException', advice='Run approved setup again.')

    def test_gate_setup_gives_retry_advice_for_a_server_error(self) -> None:
        _ = self.failed_gpu_probe_output('list-objects-v2', '503', advice='Run approved setup again.')

    def test_gate_setup_reports_one_failure_per_probe_and_one_advice_per_kind(self) -> None:
        subject, _commands = self.gate_fixture(probe_faults={'get-cost-and-usage': 'AccessDenied',
            'list-objects-v2': '503', 'get-object inputs': 'AccessDenied'})
        with redirect_stdout(StringIO()) as output:
            self.assertFalse(subject.gate_up(approve_resources=True))
        lines = output.getvalue().splitlines()
        self.assertEqual([line for line in lines if line.startswith('gate-setup probe:') and ': fail (' in line], [
            'gate-setup probe: get-cost-and-usage: fail (AccessDenied)',
            'gate-setup probe: list-objects-v2: fail (503)',
            'gate-setup probe: get-object inputs: fail (AccessDenied)',
        ])
        self.assertEqual([line for line in lines if line.startswith('gate-setup:')], [
            'gate-setup: Check the gate scaler permissions and repeat approved setup.',
            'gate-setup: Run approved setup again.',
        ])

    def test_gate_setup_rejects_a_scaler_profile_for_another_principal(self) -> None:
        subject, commands = self.gate_fixture(has_gpu_target=False, scaler_arn='fixture-other-principal')
        argv = ['deploy.py', 'up', '--workload', 'merge-gates', '--approve-resources']
        with (mock.patch.object(deploy, 'Deployment', return_value=subject), mock.patch.object(sys, 'argv', argv),
              redirect_stdout(StringIO()) as output):
            self.assertEqual(deploy.main(), 1)
        lines = output.getvalue().splitlines()
        self.assertEqual([line for line in lines if line.startswith(('gate-setup probe:', 'gate-setup:'))], [
            'gate-setup probe: get-caller-identity: fail. The scaler profile does not match the stack user.',
        ])
        self.assertNotIn('fixture-other-principal', output.getvalue())
        self.assertFalse(any('simulate-principal-policy' in args for args in commands))
        recorded = gate_probe.read_record(self.state)
        assert isinstance(recorded, gate_probe.GateProbeRecord)
        self.assertEqual(recorded.checks, (
            gate_probe.ProbeCheck(gate_probe.AccountCheck(gate_probe.AccountCheckName.CALLER_IDENTITY),
                                  gate_probe.ProbeVerdict.FAIL, gate_probe.ProbeReason.SCALER_IDENTITY_MISMATCH),
        ))

    def test_gate_setup_reports_simulator_access_denial_once_across_regions(self) -> None:
        subject, commands = self.gate_fixture(has_gpu_target=False, probe_faults={'simulate-principal-policy': 'AccessDenied'})
        self.configure_gate(
            'regions=us-east-2 us-west-2\nspot_candidate_regions=us-east-2 us-west-2')
        network = deploy.RegionalNetwork('us-west-2', 'fixture-vpc', [], 'ami-fixture')
        stack = {'region': 'us-west-2', 'subnets': [{'id': 'fixture-other-subnet', 'zone': 'us-west-2a'}]}
        with (mock.patch.object(subject, 'regional_network', return_value=network),
              mock.patch.object(subject, 'regional_gate_stack', return_value=stack), redirect_stdout(StringIO()) as output):
            self.assertTrue(subject.gate_up(approve_resources=True))
        self.assertFalse(any('terminate-instances' in args for args in commands))
        self.assertEqual(sum('simulate-principal-policy' in args for args in commands), 2)
        self.assertEqual([line for line in output.getvalue().splitlines()
                          if line.startswith('gate-setup probe: termination ') or 'SimulatePrincipalPolicy' in line], [
            'gate-setup probe: termination us-east-2 unverified.',
            'gate-setup probe: termination us-west-2 unverified.',
            'gate-setup: The administrator cannot call iam:SimulatePrincipalPolicy.',
        ])

    def test_gate_setup_simulates_the_scaler_termination_grant_without_a_box(self) -> None:
        self.check_gpu_termination_simulation('allowed', success=True)

    def test_gate_setup_fails_on_a_simulated_termination_denial(self) -> None:
        self.check_gpu_termination_simulation('implicitDeny', success=False)

    def test_gate_setup_accepts_a_resource_specific_termination_allow(self) -> None:
        self.check_gpu_termination_simulation('allowed', success=True, resource_specific=True)

    def test_gate_setup_fails_on_a_resource_specific_termination_denial(self) -> None:
        self.check_gpu_termination_simulation('explicitDeny', success=False, resource_specific=True)

    def check_gpu_termination_simulation(self, decision: str, *, success: bool, resource_specific: bool = False) -> None:
        subject, commands = self.gate_fixture(has_gpu_target=False, termination_decision=decision,
                                              resource_specific_simulation=resource_specific)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(subject.gate_up(approve_resources=True), success)
        self.assertEqual(sum('simulate-principal-policy' in args for args in commands), 1)
        self.assertFalse(any('terminate-instances' in args for args in commands))
        report = ('termination us-east-2: pass (IAM simulation)' if success
                  else 'termination us-east-2: fail (TerminationGrantDenied)')
        self.assertIn('gate-setup probe: ' + report, output.getvalue())

    def test_gate_setup_simulates_each_saved_spot_candidate_and_fails_on_a_regional_denial(self) -> None:
        subject, commands = self.gate_fixture(denied_region='us-west-2')
        self.configure_gate(
            'regions=us-east-2 us-west-2 eu-west-1 eu-west-2\n'
            + 'spot_candidate_regions=us-east-2 us-west-2 eu-west-2 us-east-1')

        def network(_api: aws_target.AwsCli, _settings: aws_config.DeploymentSettings, region: str) -> deploy.RegionalNetwork:
            return deploy.RegionalNetwork(region, 'fixture-vpc', [], 'ami-fixture')

        def stack(_api: aws_target.AwsCli, _settings: aws_config.DeploymentSettings, _outputs: dict[str, str],
                  _parameters: dict[str, str], given: deploy.RegionalNetwork,
                  _digests: deploy.RegionalStackDigests) -> dict[str, object]:
            subnets = [] if given.region == 'eu-west-2' else [{'id': 'fixture-other-subnet', 'zone': given.region + 'a'}]
            return {'region': given.region, 'subnets': subnets}

        with (mock.patch.object(subject, 'regional_network', side_effect=network),
              mock.patch.object(subject, 'regional_gate_stack', side_effect=stack), redirect_stdout(StringIO()) as output):
            self.assertFalse(subject.gate_up(approve_resources=True))
        identity = next(i for i, args in enumerate(commands) if args[6] == 'get-caller-identity' and args[2] != 'build-admin')
        simulations = [(i, args[args.index('--resource-arns') + 1].split(':')[3])
                       for i, args in enumerate(commands) if 'simulate-principal-policy' in args]
        self.assertEqual([region for _, region in simulations], ['us-east-2', 'us-west-2'])
        self.assertTrue(all(i > identity for i, _ in simulations))
        self.assertIn('terminate-instances: pass (DryRunOperation)', output.getvalue())
        self.assertIn('termination us-east-2: pass (IAM simulation)', output.getvalue())
        self.assertIn('termination us-west-2: fail (TerminationGrantDenied)', output.getvalue())

    def test_gpu_trial_command_requires_approval_and_checks_credentials(self) -> None:
        subject = deploy.Deployment(self.settings, self.state, self.root / 'output')
        folder = self.root / 'trial-alias'
        folder.symlink_to(self.root, target_is_directory=True)
        with mock.patch.object(gpu_size_trial, 'start_service', return_value=0) as service:
            with self.assertRaisesRegex(deploy.DeploymentRefused, '--approve-resources'):
                _ = subject.gpu_size_trial(self.root / 'trial')
            service.assert_not_called()
            with mock.patch.object(subject, '_check_credentials') as credentials:
                self.assertTrue(subject.gpu_size_trial(folder, approve_resources=True))
            credentials.assert_called_once_with()
        service.assert_called_once()
        assert service.call_args is not None
        self.assertEqual(service.call_args.args[0], folder)

    def test_gpu_trial_cli_routes_the_approved_run(self) -> None:
        folder = self.root / 'trial'
        argv = ['deploy.py', 'gpu-size-trial', 'run', '--trial-folder', str(folder),
                '--settings', str(self.settings), '--directory', str(self.state), '--approve-resources']
        with (mock.patch.object(sys, 'argv', argv),
              mock.patch.object(deploy.Deployment, 'gpu_size_trial', return_value=True) as run):
            self.assertEqual(deploy.main(), 0)
        run.assert_called_once_with(folder, approve_resources=True)

    def test_gpu_trial_permission_preserves_normal_gate_scope(self) -> None:
        source = ledger.mapping(cast(object, json.loads(deploy.TEMPLATE.read_text())))
        resources = ledger.mapping(source['Resources'])
        properties = [ledger.mapping(ledger.mapping(resources[name])['Properties'])
                      for name in ('GateScalerPolicy', 'GateGpuTrialScalerPolicy')]
        statements = tuple(row for policy in properties for row in ledger.rows(ledger.mapping(policy['PolicyDocument']), 'Statement')
                           if 'Fn::If' not in row)
        scopes = [ledger.mapping(ledger.mapping(row['Condition'])['StringEquals'])
                  for row in statements if row.get('Condition')
                  and 'ec2:InstanceType' in ledger.mapping(ledger.mapping(row['Condition']).get('StringEquals', {}))]
        self.assertEqual([row['ec2:InstanceType'] for row in scopes],
                         [{'Ref': 'GateInstanceTypes'}, {'Ref': 'GateGpuInstanceTypes'}])
        for row in scopes:
            self.assertEqual(row['aws:RequestTag/Workload'], 'cloud-claude-gate')
            self.assertEqual(row['ec2:InstanceMarketType'], 'spot')
        regional = ledger.mapping(deploy.region_gate_template()['Resources'])
        self.assertNotIn('GateScalerPolicy', regional)
        self.assertNotIn('GateGpuTrialScalerPolicy', regional)
        gpu_statements = ledger.rows(ledger.mapping(properties[1]['PolicyDocument']), 'Statement')
        gpu_instance = gpu_statements[0]
        self.assertEqual(ledger.mapping(ledger.mapping(gpu_instance['Condition'])['Null'])['aws:RequestTag/GpuSizeTrial'], 'false')
        self.assertIn('ec2:InstanceProfile', ledger.mapping(ledger.mapping(gpu_instance['Condition'])['ArnEquals']))

    def test_gate_deployment_rejects_a_change_to_a_rust_resource(self) -> None:
        subject, commands = self.gate_fixture('Instance')
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'Rust resource'):
            _ = subject.gate_up(approve_resources=True)
        self.assertFalse(any('execute-change-set' in args for args in commands))
        self.assertTrue(any('delete-change-set' in args for args in commands))
        self.assertFalse((self.state / 'gate-deployment.json').exists())


    def test_update_refuses_replacement_for_each_protected_resource_type(self) -> None:
        subject = self.update_fixture()
        for logical, kind in (
                ('Instance', 'AWS::EC2::Instance'), ('DataVolume', 'AWS::EC2::Volume'),
                ('DataAttachment', 'AWS::EC2::VolumeAttachment'), ('Interface', 'AWS::EC2::NetworkInterface'),
                ('InterfaceAttachment', 'AWS::EC2::NetworkInterfaceAttachment'), ('Address', 'AWS::EC2::EIP'),
                ('Association', 'AWS::EC2::EIPAssociation'), ('Budget', 'AWS::Budgets::Budget')):
            for replacement in ('True', 'Conditional'):
                with self.subTest(kind=kind, replacement=replacement):
                    _ = (self.root / 'update-plan.json').write_text(json.dumps({'Changes': [{'ResourceChange': {
                        'LogicalResourceId': logical, 'ResourceType': kind, 'Replacement': replacement}}]}))
                    before = len(self.commands('execute-change-set'))
                    with self.assertRaisesRegex(deploy.DeploymentRefused, logical) as failure:
                        _ = subject.apply(approve_resources=True)
                    self.assertIn(kind, str(failure.exception))
                    self.assertIn('deploy.py down --approve-destroy, then deploy.py up --approve-resources', str(failure.exception))
                    self.assertEqual(len(self.commands('execute-change-set')), before)
                    self.assertFalse((self.root / 'update-change-set.json').exists())

    def test_update_refuses_removal_for_each_protected_type(self) -> None:
        subject = self.update_fixture()
        for kind in ('AWS::EC2::Instance', 'AWS::EC2::Volume', 'AWS::EC2::VolumeAttachment',
                     'AWS::EC2::NetworkInterface', 'AWS::EC2::NetworkInterfaceAttachment',
                     'AWS::EC2::EIP', 'AWS::EC2::EIPAssociation', 'AWS::Budgets::Budget'):
            with self.subTest(kind=kind):
                _ = (self.root / 'update-plan.json').write_text(json.dumps({'Changes': [{'ResourceChange': {
                    'LogicalResourceId': 'OldResource', 'ResourceType': kind, 'Action': 'Remove'}},
                    {'ResourceChange': {'LogicalResourceId': 'NewResource', 'ResourceType': kind, 'Action': 'Add'}}]}))
                before = len(self.commands('execute-change-set'))
                with self.assertRaisesRegex(deploy.DeploymentRefused, 'OldResource') as failure:
                    _ = subject.apply(approve_resources=True)
                self.assertIn(kind, str(failure.exception))
                self.assertIn('deploy.py down --approve-destroy, then deploy.py up --approve-resources', str(failure.exception))
                self.assertEqual(len(self.commands('execute-change-set')), before)
                self.assertFalse((self.root / 'update-change-set.json').exists())

    def test_unexecuted_update_cleanup_preserves_each_refusal_and_reports_delete_failure(self) -> None:
        subject = self.update_fixture()
        for cause in ('spending check refused', 'preparation check refused', 'create wait failed', 'protected removal'):
            for delete_fails in (False, True):
                with self.subTest(cause=cause, delete_fails=delete_fails):
                    if cause == 'protected removal':
                        _ = (self.root / 'update-plan.json').write_text(json.dumps({'Changes': [{'ResourceChange': {
                            'LogicalResourceId': 'Protected', 'ResourceType': 'AWS::EC2::Instance', 'Action': 'Remove'}}]}))
                    if cause == 'create wait failed':
                        (self.root / 'fail-wait-change-set-create-complete').touch()
                    if delete_fails:
                        (self.root / 'fail-down-delete-change-set').touch()
                    settings = aws_config.read_deployment_settings(self.settings)
                    prepared = subject.prepare()
                    before = len(self.commands('execute-change-set'))
                    with mock.patch.object(subject, '_before_request', side_effect=deploy.DeploymentRefused(cause)):
                        with self.assertRaises((deploy.DeploymentRefused, aws_target.AwsUnavailable)) as failure:
                            subject._update_stack(settings, prepared.parameters)  # pyright: ignore[reportPrivateUsage]
                    expected = {'create wait failed': 'fixture wait failed', 'protected removal': 'Protected'}.get(cause, cause)
                    self.assertIn(expected, str(failure.exception))
                    args = cast(list[str], self.commands('create-change-set')[-1]['args'])
                    name = args[args.index('--change-set-name') + 1]
                    self.assertEqual('unexecuted change set remains: ' + name in str(failure.exception), delete_fails)
                    self.assertEqual((self.root / 'update-change-set.json').exists(), delete_fails)
                    self.assertEqual(len(self.commands('execute-change-set')), before)
                    for filename in ('update-change-set.json', 'update-plan.json', 'fail-down-delete-change-set',
                                     'fail-wait-change-set-create-complete'):
                        (self.root / filename).unlink(missing_ok=True)

    def test_lost_update_creation_response_discards_the_recovered_change_set(self) -> None:
        subject = self.update_fixture()
        execute = subject.execute
        before = len(self.commands('execute-change-set'))
        def lose_response(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                          check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            result = execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
            if 'create-change-set' in arguments:
                return subprocess.CompletedProcess(arguments, 1, '', 'update creation response lost')
            return result
        subject.execute = lose_response
        with self.assertRaisesRegex(aws_target.AwsUnavailable, 'update creation response lost'):
            _ = subject.apply(approve_resources=True)
        self.assertTrue(self.commands('describe-change-set'))
        self.assertTrue(self.commands('delete-change-set'))
        self.assertFalse((self.root / 'update-change-set.json').exists())
        self.assertEqual(len(self.commands('execute-change-set')), before)

    def test_cleanup_value_and_os_errors_preserve_the_original_refusal(self) -> None:
        subject = self.update_fixture()
        settings = aws_config.read_deployment_settings(self.settings)
        prepared = subject.prepare()
        api = subject._api(settings)  # pyright: ignore[reportPrivateUsage]
        call = api.call
        for error in (ValueError('cleanup value error'), OSError('cleanup OS error')):
            with self.subTest(error=type(error).__name__):
                def fail_delete(operation: str, *arguments: str, service: str = 'ec2',
                                timeout_s: float | None = None) -> dict[str, object]:
                    if operation == 'delete-change-set':
                        raise error
                    return call(operation, *arguments, service=service, timeout_s=timeout_s)
                with (mock.patch.object(subject, '_api', return_value=api),
                      mock.patch.object(api, 'call', side_effect=fail_delete),
                      mock.patch.object(subject, '_before_request', side_effect=deploy.DeploymentRefused('original spending refusal'))):
                    with self.assertRaisesRegex(deploy.DeploymentRefused, 'original spending refusal') as failure:
                        subject._update_stack(settings, prepared.parameters)  # pyright: ignore[reportPrivateUsage]
                args = cast(list[str], self.commands('create-change-set')[-1]['args'])
                name = args[args.index('--change-set-name') + 1]
                self.assertIn('unexecuted change set remains: ' + name, str(failure.exception))
                self.assertNotIn(str(error), str(failure.exception))
                self.assertTrue((self.root / 'update-change-set.json').exists())
                (self.root / 'update-change-set.json').unlink()


    def test_foundation_preparation_refusal_discards_the_reservation(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        confirm = subject._confirm_preparation  # pyright: ignore[reportPrivateUsage]
        def refuse(settings: aws_config.DeploymentSettings, parameters: dict[str, str], *, renew: bool = False) -> None:
            if renew:
                raise deploy.DeploymentRefused('foundation preparation changed')
            confirm(settings, parameters, renew=renew)
        with mock.patch.object(subject, '_confirm_preparation', side_effect=refuse):
            with self.assertRaisesRegex(deploy.DeploymentRefused, 'foundation preparation changed'):
                _ = subject.apply(approve_resources=True)
        self.assertFalse((self.root / 'change-set.json').exists())
        self.assertFalse(self.commands('deploy'))

    def test_failure_redacts_the_prepared_address_after_the_private_file_changes(self) -> None:
        subject = self.update_fixture()
        notification = self.notification_file.read_text().strip()
        execute = subject.execute
        for remove in (False, True):
            with self.subTest(remove=remove):
                def fail(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                         check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    if 'execute-change-set' in arguments:
                        if remove:
                            self.notification_file.unlink()
                        else:
                            _ = self.notification_file.write_text('changed@example.invalid\n')
                        return subprocess.CompletedProcess(arguments, 1, '', 'update refused ' + notification)
                    return execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
                subject.execute = fail
                try:
                    with self.assertRaisesRegex(aws_target.AwsUnavailable, 'update refused <notification address>') as failure:
                        _ = subject.apply(approve_resources=True)
                    self.assertNotIn(notification, str(failure.exception))
                    self.assertIn('stack status: UPDATE_COMPLETE', str(failure.exception))
                finally:
                    _ = self.notification_file.write_text(notification + '\n')
                    subject.execute = execute

    def test_create_reservation_failures_report_events_reason_status_and_redact(self) -> None:
        subject = self.deployment()
        prepared = subject.prepare()
        notification = prepared.parameters['NotificationEmail']
        execute = subject.execute
        for operation in ('create-change-set', 'wait'):
            with self.subTest(operation=operation):
                failed = False
                def fail(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                         check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    nonlocal failed
                    if failed and 'describe-stacks' in arguments:
                        return subprocess.CompletedProcess(arguments, 0, json.dumps({'Stacks': [{'StackStatus': 'CREATE_FAILED'}]}), '')
                    if operation == 'create-change-set' and operation in arguments:
                        failed = True
                        return subprocess.CompletedProcess(arguments, 1, '',
                            'reservation refused ' + notification + ' AWS_SESSION_TOKEN=fixture-token')
                    result = execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
                    if 'describe-change-set' in arguments and result.returncode == 0:
                        reply = ledger.mapping(cast(object, json.loads(result.stdout)))
                        reply['StatusReason'] = 'reservation reason ' + notification
                        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
                    if operation == 'wait' and 'wait' in arguments:
                        failed = True
                        return subprocess.CompletedProcess(arguments, 1, '',
                            'reservation refused ' + notification + ' AWS_SESSION_TOKEN=fixture-token')
                    return result
                _ = (self.root / 'describe-stack-events.json').write_text(json.dumps({'StackEvents': [{
                    'Timestamp': datetime.fromtimestamp(self.now, UTC).isoformat(), 'LogicalResourceId': 'Foundation',
                    'ResourceType': 'AWS::CloudFormation::Stack', 'ResourceStatus': 'CREATE_FAILED',
                    'ResourceStatusReason': 'event reason ' + notification}]}))
                subject.execute = fail
                try:
                    with self.assertRaises(aws_target.AwsUnavailable) as failure:
                        _ = subject.apply(approve_resources=True)
                    reason = subject.admin.redact(str(failure.exception))
                    self.assertIn('reservation refused <notification address>', reason)
                    self.assertIn('AWS_SESSION_TOKEN=[redacted]', reason)
                    self.assertNotIn('fixture-token', reason)
                    self.assertIn('Foundation (AWS::CloudFormation::Stack): CREATE_FAILED: event reason <notification address>', reason)
                    self.assertIn('stack status: CREATE_FAILED', reason)
                    self.assertNotIn(notification, reason)
                    if operation == 'wait':
                        self.assertIn('reservation reason <notification address>', reason)
                        self.assertFalse((self.root / 'change-set.json').exists())
                    events = cast(list[str], self.commands('describe-stack-events')[-1]['args'])
                    expected = 'fixture-build' if operation == 'create-change-set' else ledger.text(
                        aws_config.deployment_record(self.state), 'stack_id')
                    self.assertEqual(events[events.index('--stack-name') + 1], expected)
                finally:
                    subject.execute = execute


    def test_foundation_apply_never_requests_builder_compute(self) -> None:
        subject = self.update_fixture()
        outputs = subject.apply(approve_resources=True)
        self.assertNotIn('ComputeEnabled', outputs)
        self.assertNotIn('InstanceId', outputs)
        self.assertTrue(outputs['BudgetName'])
        self.assertFalse(self.commands('start-instances'))
        self.assertFalse(self.commands('run-instances'))
        self.assertFalse(ledger.CostLedger(self.state).pending_compute_requests())
        self.assertTrue(ledger.CostLedger(self.state).read_snapshot().inventories[-1].builder_removed)

    def test_empty_update_is_success_without_execution_or_compute_request(self) -> None:
        subject = self.update_fixture()
        _ = (self.root / 'update-plan.json').write_text(json.dumps({
            'Status': 'FAILED', 'StatusReason': "The submitted information didn't contain changes."}))
        (self.root / 'fail-wait-change-set-create-complete').touch()
        before = len(self.commands('execute-change-set'))
        outputs = subject.apply(approve_resources=True)
        self.assertNotIn('ComputeEnabled', outputs)
        self.assertEqual(len(self.commands('execute-change-set')), before)
        self.assertFalse(ledger.CostLedger(self.state).pending_compute_requests())
        self.assertTrue(self.commands('delete-change-set'))


    def test_change_set_creation_and_execution_failures_report_the_cause(self) -> None:
        subject = self.update_fixture()
        notification = self.notification_file.read_text().strip()
        for operation in ('create-change-set', 'execute-change-set'):
            with self.subTest(operation=operation):
                execute = subject.execute
                def fail(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                         check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    result = execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
                    if operation in arguments:
                        return subprocess.CompletedProcess(arguments, 1, '', 'operation refused ' + notification)
                    return result
                _ = (self.root / 'update-plan.json').write_text(json.dumps({'StatusReason': 'change set refused ' + notification}))
                subject.execute = fail
                try:
                    with self.assertRaises(aws_target.AwsUnavailable) as failure:
                        _ = subject.apply(approve_resources=True)
                    reason = str(failure.exception)
                    self.assertIn('operation refused <notification address>', reason)
                    self.assertIn('change set refused <notification address>', reason)
                    self.assertIn('stack status: UPDATE_COMPLETE', reason)
                    self.assertNotIn(notification, reason)
                finally:
                    subject.execute = execute
                # Reconcile any execution request before the next case.
                _ = subject.prepare()


    def test_saved_image_is_empty_when_its_image_is_gone(self) -> None:
        subject = self.deployment()
        _ = (self.state / 'image.json').write_text(json.dumps({'image_id': 'ami-cache', 'system': SYSTEM}))
        with mock.patch.object(aws_target.AwsCli, 'call', side_effect=aws_target.AwsUnavailable('InvalidAMIID.NotFound')):
            self.assertEqual(box.saved_image(subject._api(aws_config.read_deployment_settings(self.settings)), self.state), {})  # pyright: ignore[reportPrivateUsage]

    def test_create_saved_image_tags_the_image_and_snapshot(self) -> None:
        subject = self.deployment()
        api = subject._api(aws_config.read_deployment_settings(self.settings))  # pyright: ignore[reportPrivateUsage]
        image = box.create_saved_image(api, self.state, 'i-fixture', name='fixture-image',
            tags=[{'Key': box.IMAGE_OWNER_TAG, 'Value': 'true'}], identity={}, exclude_devices=('/dev/sdf',))
        self.assertEqual(image, {'image_id': 'ami-cache'})
        self.assertEqual(box.saved_image(api, self.state)['ImageId'], 'ami-cache')


    def test_down_keeps_and_reports_the_saved_image(self) -> None:
        _ = self.removal_fixture()
        path = self.state / 'image.json'
        _ = path.write_text(json.dumps({'image_id': 'ami-cache', 'system': SYSTEM, 'idle_stop_system': SYSTEM}))
        before = path.read_bytes()
        gate_directory = self.state / 'gate-image'
        gate_directory.mkdir()
        _ = (gate_directory / 'image.json').write_text('{"image_id":"ami-gate"}')
        with mock.patch.object(box, 'cleanup_images') as cleanup:
            success, output = self.down_output()
        self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(cast(tuple[object, ...], cleanup.call_args.args)[1:], ({}, gate_box.GATE_IMAGE_OWNER_TAG))
        self.assertFalse(gate_directory.exists())
        self.assertTrue(success, output)
        self.assertIn('down: image kept: ami-cache; next up starts from it', output)
        self.assertEqual(path.read_bytes(), before)


    def prepare_running_costs(self) -> tuple[int, ledger.CostLedger]:
        start = int(datetime(2026, 1, 15, tzinfo=UTC).timestamp())
        self.replace('daily_limit_usd', '20')
        self.costs({'fallback_usd_per_hour': '0.10', 'fixed_usd_per_month': '300',
                    'price_max_age_seconds': '86400', 'observation_max_age_seconds': '86400',
                    'cost_explorer_read_usd': '0'})
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('storage', Decimal('300')),),
                                                        'retained storage'))
        book.record_price(ledger.PriceSample(start, Decimal('0.483'), 'fresh Spot price'))
        book.observe(ledger.InstanceObservation(start, 'running', 'confirmed running instance'))
        self.set_clock(start + 23 * 3600)
        return start, book

    def test_unconfirmed_request_adds_costs_without_reversing_a_limit_refusal(self) -> None:
        start, book = self.prepare_running_costs()
        before = book.guard(self.settings, 'deployment', self.now, notify=lambda _event: True)
        self.assertIsInstance(before, ledger.SpendingRefused)
        self.assertIsInstance(before.evidence.costs, ledger.CostEstimate)
        if not isinstance(before.evidence.costs, ledger.CostEstimate):
            self.fail('the guard must price the running instance')
        book.record_compute_request(ledger.ComputeRequest(start, 'fixture-build', 'us-east-1', Decimal('0.5')))
        after = book.guard(self.settings, 'deployment', self.now, notify=lambda _event: True)
        self.assertIsInstance(after, ledger.SpendingRefused)
        self.assertIsInstance(after.evidence.costs, ledger.CostEstimate)
        if not isinstance(after.evidence.costs, ledger.CostEstimate):
            self.fail('the guard must price the running instance and request')
        request_usd = Decimal('0.5') * Decimal(23)
        self.assertGreaterEqual(after.evidence.costs.daily.usd, before.evidence.costs.daily.usd + request_usd)
        self.assertGreaterEqual(after.evidence.costs.monthly.usd, before.evidence.costs.monthly.usd + request_usd)
        self.assertEqual([r for r in after.evidence.costs.intervals if r.source != 'unconfirmed compute request'],
                         list(before.evidence.costs.intervals))

    def test_unconfirmed_request_keeps_confirmed_running_uptime(self) -> None:
        start, book = self.prepare_running_costs()
        before = self.instance_intervals(self.now)
        book.record_compute_request(ledger.ComputeRequest(start, 'fixture-build', 'us-east-1', Decimal('0.5')))
        after = self.instance_intervals(self.now)
        self.assertEqual(sum(r[3] for r in before), 23 * 3600)
        self.assertEqual(sum(r[3] for r in after), sum(r[3] for r in before))
        self.assertEqual(after, [*before, (start, self.now, Decimal('11.5'), 0)])


    def removal_fixture(self, *, save_node: bool = True) -> dict[str, object]:
        _ = self.update_fixture()
        state = remote.read_record(self.root / 'fake-stack.json')
        outputs = ledger.mapping(state['outputs'])
        outputs.update(InstanceId='i-fixture', DataVolumeId='vol-created', ComputeEnabled='true')
        state['instance_state'] = 'stopped'
        write_text_atomic(self.root / 'fake-stack.json', json.dumps(state))
        saved = deployment_record = remote.read_record(self.state / 'deployment.json')
        saved.update(outputs={**outputs, 'DataVolumeId': ''}, data_volume_id='vol-created')
        write_text_atomic(self.state / 'deployment.json', json.dumps(saved))
        _ = (self.root / 'volume-owner').write_text(str(deployment_record['deployment_id']))
        write_text_atomic(self.state / 'up.json', json.dumps({'box-join': {}} if save_node else {}))
        (self.state / 'up').mkdir(exist_ok=True)
        (self.state / 'up/controller.lock').touch()
        ledger.CostLedger(self.state).observe(ledger.InstanceObservation(self.now, 'terminated', 'historical builder fixture'), confirms_transition=True)
        self.fake_commands(f'#!{sys.executable}\n' + r'''import json, os, pathlib, sys
root = pathlib.Path(os.environ['DEPLOY_FIXTURE'])
p = root / 'controller-enabled'
args = sys.argv[1:]
with (root / 'systemctl-commands').open('a') as log: log.write(' '.join(args) + '\n')
if 'disable' in args:
    if (root / 'fail-service').exists(): sys.exit(1)
    p.unlink(missing_ok=True)
elif 'enable' in args: p.touch()
elif 'is-enabled' in args: print('enabled' if p.exists() else 'disabled')
elif 'is-active' in args: print('active' if p.exists() else 'inactive')
else: raise AssertionError(args)
''', 'systemctl')
        (self.root / 'controller-enabled').touch()
        devices: dict[str, object] = {'devices': [{'id': 'device-box', 'hostname': 'aws-build', 'tags': ['tag:build-aws']},
                                                {'id': 'device-other', 'hostname': 'other', 'tags': []}]}
        if save_node:
            saved = remote.read_record(self.state / 'deployment.json')
            saved.update(tailscale_node_id='device-box', tailscale_instance_id='i-fixture')
            _ = (self.state / 'deployment.json').write_text(json.dumps(saved))
        policy = FakeTailnet()
        def tailnet(method: str, endpoint: str, body: bytes, headers: dict[str, str], timeout: float) -> box.PolicyReply:
            self.assertGreater(timeout, 0)
            self.assertIn('Content-Type' if endpoint == 'oauth/token' else 'Authorization', headers)
            self.assertTrue(body if method == 'POST' else not body)
            if (self.root / 'fail-tailnet').exists():
                raise box.BoxRefused('fixture device removal failure')
            if endpoint == 'oauth/token':
                return box.PolicyReply({'access_token': 'fixture-token'})
            if method == 'GET' and endpoint == 'tailnet/-/devices':
                return box.PolicyReply(devices)
            if method == 'DELETE' and endpoint.startswith('device/'):
                device_id = endpoint.removeprefix('device/')
                self.assertTrue(any(row['id'] == device_id for row in ledger.rows(devices, 'devices')))
                devices['devices'] = [row for row in ledger.rows(devices, 'devices') if row['id'] != device_id]
                return box.PolicyReply({})
            if '/acl' in endpoint or '/keys' in endpoint:
                return policy(method, endpoint, body, headers, timeout)
            raise AssertionError((method, endpoint))
        _ = cast(object, self.enterContext(mock.patch.object(box, 'tailnet_request', side_effect=tailnet)))
        _ = cast(object, self.enterContext(mock.patch.object(target_config, 'ssh_target_state_directory', return_value=self.state)))
        return devices


    def down_output(self) -> tuple[bool, str]:
        output = StringIO()
        with redirect_stdout(output):
            success = self.deployment().down(approve_destroy=True)
        return success, output.getvalue()


    def test_down_never_joined_box_reports_no_device_without_deleting_one(self) -> None:
        devices = self.removal_fixture(save_node=False)
        devices['devices'] = [row for row in ledger.rows(devices, 'devices') if row['id'] == 'device-other']
        stages = remote.read_record(self.state / 'up.json')
        self.assertNotIn('box-join', stages)
        self.assertNotIn('tailscale_node_id', remote.read_record(self.state / 'deployment.json'))
        with mock.patch.object(box.Tailnet, 'delete_device') as delete:
            success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertIn('tailscale-device: no device', output)
        delete.assert_not_called()
        self.assertEqual([row['id'] for row in ledger.rows(devices, 'devices')], ['device-other'])
        self.assertFalse((self.state / 'deployment.json').exists())

    def test_down_removes_rebuilt_foundation_without_a_data_volume(self) -> None:
        _ = self.removal_fixture()
        success, output = self.down_output()
        self.assertTrue(success, output)
        subject = self.deployment()
        _ = subject.prepare()
        outputs = subject.apply(approve_resources=True)
        self.assertNotIn('ComputeEnabled', outputs)
        self.assertNotIn('InstanceId', outputs)
        self.assertNotIn('DataVolumeId', outputs)
        saved = remote.read_record(self.state / 'deployment.json')
        self.assertFalse(saved.get('data_volume_id'))
        before = len(self.commands())
        success, output = self.down_output()
        self.assertTrue(success, output)
        operations = [cast(list[str], row['args'])[5] for row in self.commands()[before:]]
        self.assertIn('delete-stack', operations)
        self.assertIn('delete-budget', operations)
        self.assertNotIn('describe-volumes', operations)
        self.assertNotIn('delete-volume', operations)
        self.assertNotIn('update-stack', operations)
        self.assertFalse((self.root / 'fake-stack.json').exists())
        self.assertFalse((self.state / 'deployment.json').exists())
        self.assertTrue((self.state / 'down-complete.json').exists())

    def test_down_second_node_identity_check_refuses_without_a_journal(self) -> None:
        devices = self.removal_fixture(save_node=False)
        with mock.patch.object(box.Tailnet, 'call', side_effect=[box.PolicyReply({'devices': []}),
                                                               box.PolicyReply(devices)]) as check, \
             mock.patch.object(box.Tailnet, 'delete_device') as delete:
            success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('tailscale-device: refused: missing saved Tailscale node id; run deploy.py up', output)
        self.assertEqual(check.call_args_list, [mock.call('GET', 'devices'), mock.call('GET', 'devices')])
        delete.assert_not_called()
        self.assertFalse((self.state / 'down.json').exists())
        self.assertTrue((self.state / 'deployment.json').exists())
        self.assertEqual([row['id'] for row in ledger.rows(devices, 'devices')], ['device-box', 'device-other'])

    def test_down_checks_each_required_saved_identity_before_changes(self) -> None:
        _ = self.removal_fixture()
        path = self.state / 'deployment.json'
        original = path.read_text()
        fields = ('data_volume_id', 'budget_name', 'deployment_id', 'tailscale_instance_id',
                  'scaler_user', 'name', 'arn', 'user_id')
        for field in fields:
            with self.subTest(field=field):
                saved = ledger.mapping(cast(object, json.loads(original)))
                parent = ledger.mapping(saved['scaler_user']) if field in ('name', 'arn', 'user_id') else saved
                del parent[field]
                _ = path.write_text(json.dumps(saved))
                before = {file: file.read_bytes() for file in self.state.rglob('*') if file.is_file()}
                success, output = self.down_output()
                self.assertFalse(success, output)
                self.assertIn('identity: refused:', output)
                self.assertIn('run deploy.py up once to record it', output)
                self.assertEqual({file: file.read_bytes() for file in self.state.rglob('*') if file.is_file()}, before)
                self.assertFalse((self.state / 'down.json').exists())
                self.assertFalse((self.root / 'systemctl-commands').exists())
        _ = path.write_text(original)

    def test_down_dry_run_changes_nothing_and_needs_no_credentials(self) -> None:
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with mock.patch.dict(os.environ, {'AWS_SHARED_CREDENTIALS_FILE': ''}), redirect_stdout(StringIO()) as output:
            self.assertTrue(self.deployment().down())
        self.assertIn('/home build cache is lost', output.getvalue())
        self.assertIn('admin.gpg, and the box image', output.getvalue())
        self.assertIn('--approve-destroy', output.getvalue())
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)

    def test_down_removes_resources_and_preserves_spend_and_other_profiles(self) -> None:
        devices = self.removal_fixture()
        _ = write_config_fixture(self.root / 'build_targets.conf', defaults=mac_target_settings())
        _ = write_config_fixture(self.root / 'mac_test.conf', defaults='')
        self.assertEqual([target.name for target in target_config.read_targets()], ['mac', 'natedev'])
        original = self.settings.read_bytes()
        before = ledger.CostLedger(self.state).read_snapshot()
        with self.credentials.open('a') as handle:
            _ = handle.write('\n[unrelated]\naws_access_key_id=other-id\naws_secret_access_key=other-value\n')
        (self.root / 'admin.gpg').touch()
        charges = self.state / 'charges/day.json'
        charges.parent.mkdir(exist_ok=True)
        _ = charges.write_text('retained charge evidence')
        with mock.patch.object(box, 'close_routing', wraps=box.close_routing) as close:
            success, output = self.down_output()
        close.assert_called_once_with(self.settings)
        self.assertTrue(success, output)
        self.assertIn('data volume; the /home build cache is lost', output)
        self.assertNotIn('down: image kept:', output)
        self.assertIn('retained Budget', output)
        self.assertEqual([row['hostname'] for row in ledger.rows(devices, 'devices')], ['other'])
        self.assertEqual(admin_secrets.read_profiles(self.credentials).sections(), ['unrelated'])
        self.assertEqual(self.credentials.stat().st_mode & 0o777, 0o600)
        self.assertEqual(charges.read_text(), 'retained charge evidence')
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertFalse(remote.read_record(self.state / 'routing.json')['enabled'])
        after = ledger.CostLedger(self.state).read_snapshot()
        self.assertEqual(after.reconciliations, before.reconciliations)
        self.assertEqual(after.events, before.events)
        self.assertEqual(after.prices, before.prices)
        self.assertEqual(after.observations[-1].state, 'terminated')
        self.assertFalse((self.state / 'deployment.json').exists())
        self.assertFalse((self.state / 'up.json').exists())
        self.assertTrue((self.root / 'admin.gpg').exists())
        operations = [cast(list[str], row['args'])[5] for row in self.commands()]
        self.assertNotIn('update-stack', operations)
        self.assertLess(operations.index('delete-stack'), operations.index('delete-volume'))
        before_commands = len(self.commands())
        before_service = (self.root / 'systemctl-commands').read_text()
        success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertTrue(all(line.endswith(': present') for line in self.previous_output(output, 'down').splitlines()), output)
        later = [cast(list[str], row['args'])[5] for row in self.commands()[before_commands:]]
        self.assertFalse(any(operation.startswith(('delete-', 'update-')) for operation in later))
        self.assertNotIn('disable', (self.root / 'systemctl-commands').read_text()[len(before_service):])
        self.assertNotIn('fixture-admin-secret', output)
        self.assertNotIn('fixture-secret-only-in-credentials', output)

    def test_down_resumes_after_each_removal_failure(self) -> None:
        devices = self.removal_fixture()
        self.assertIn('devices', devices)
        failures = (('fail-service', 'routing-off'), ('fail-tailnet', 'tailscale-device'),
                    ('fail-down-delete-access-key', 'scaler-keys'),
                    ('fail-down-delete-stack', 'foundation'), ('fail-down-delete-volume', 'retained-resources'),
                    ('fail-down-delete-budget', 'retained-resources'))
        for filename, stage in failures:
            with self.subTest(stage=stage, command=filename):
                (self.root / filename).touch()
                success, output = self.down_output()
                self.assertFalse(success, output)
                self.assertIn(stage + ': refused:', output)
                self.assertTrue((self.state / 'deployment.json').exists())
                self.assertFalse(remote.read_record(self.state / 'routing.json')['enabled'])
                (self.root / filename).unlink()
        success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertEqual(len(self.commands('update-stack')), 0)
        self.assertEqual(len(self.commands('delete-stack')), 1)
        self.assertEqual(len(self.commands('delete-volume')), 1)

    def test_down_refuses_a_live_claim_after_disabling_routing(self) -> None:
        devices = self.removal_fixture()
        self.assertIn('devices', devices)
        before = len(self.commands())
        claim = write_aws_claim(self.state, kept=False)
        success, output = self.down_output()
        self.assertFalse(success)
        self.assertIn('AWS claims remain live', output)
        self.assertFalse(remote.read_record(self.state / 'routing.json')['enabled'])
        self.assertFalse((self.root / 'controller-enabled').exists())
        self.assertEqual([cast(list[str], row['args'])[5] for row in self.commands()[before:]],
                         ['get-caller-identity', 'describe-stacks'])
        self.assertTrue(claim.exists())
        claim.unlink()
        success, output = self.down_output()
        self.assertTrue(success, output)

    def test_down_preserves_a_kept_claim_and_completes(self) -> None:
        _ = self.removal_fixture()
        path = write_aws_claim(self.state, kept=True)
        original = path.read_bytes()
        with mock.patch.object(box, 'running_claims', wraps=box.running_claims) as claims:
            success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertEqual(claims.call_count, 2)
        self.assertEqual(path.read_bytes(), original)

    def test_down_ignores_a_reservation_and_preserves_its_file(self) -> None:
        _ = self.removal_fixture()
        path = write_aws_claim(self.state, kept=False, remote=False)
        original = path.read_bytes()
        with mock.patch.object(box, 'running_claims', wraps=box.running_claims) as claims:
            success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertEqual(claims.call_count, 2)
        self.assertEqual(path.read_bytes(), original)

    def test_down_refuses_a_running_claim_before_device_removal(self) -> None:
        devices = self.removal_fixture()
        read_claims = box.running_claims
        calls = 0
        def claims(paths: mac_test.StatePaths) -> tuple[mac_test.HeldClaim, ...]:
            nonlocal calls
            calls += 1
            if calls == 2:
                _ = write_aws_claim(self.state, kept=False)
            return read_claims(paths)
        with mock.patch.object(box, 'running_claims', side_effect=claims):
            success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('tailscale-device: refused: AWS claims remain live', output)
        self.assertEqual(calls, 2)
        self.assertEqual(len(ledger.rows(devices, 'devices')), 2)
        self.assertTrue((self.state / 'claims/job.json').exists())


    def test_down_verifies_immutable_account_region_and_stack_before_any_change(self) -> None:
        devices = self.removal_fixture()
        self.assertIn('devices', devices)
        path = self.state / 'deployment.json'
        original = path.read_bytes()
        saved = remote.read_record(path)
        for field, value in (('account_id', '999999999999'), ('region', 'us-west-2'),
                             ('stack_id', 'arn:aws:cloudformation:us-east-1:123456789012:stack/fixture-build/other')):
            with self.subTest(field=field):
                _ = path.write_text(json.dumps({**saved, field: value}))
                files = {p: p.read_bytes() for p in self.state.rglob('*') if p.is_file()}
                before = len(self.commands())
                success, output = self.down_output()
                self.assertFalse(success, output)
                self.assertIn('identity: refused:', output)
                self.assertEqual({p: p.read_bytes() for p in self.state.rglob('*') if p.is_file()}, files)
                self.assertTrue((self.root / 'controller-enabled').exists())
                self.assertFalse(any(cast(list[str], r['args'])[5].startswith(('delete-', 'update-')) for r in self.commands()[before:]))
        path.unlink()
        success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('saved deployment identity', output)
        _ = path.write_bytes(original)
        success, output = self.down_output()
        self.assertTrue(success, output)
        mutations = [cast(list[str], r['args']) for r in self.commands() if cast(list[str], r['args'])[5] in ('update-stack', 'delete-stack')]
        self.assertTrue(mutations)
        self.assertTrue(all(args[args.index('--stack-name') + 1] == saved['stack_id'] for args in mutations))

    def test_down_deletes_only_the_saved_node_and_refuses_missing_node_identity(self) -> None:
        devices = self.removal_fixture()
        path = self.state / 'deployment.json'
        saved = remote.read_record(path)
        del saved['tailscale_node_id']
        _ = path.write_text(json.dumps(saved))
        success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('run deploy.py up once to record it', output)
        self.assertEqual([row['id'] for row in ledger.rows(devices, 'devices')], ['device-box', 'device-other'])
        devices['devices'] = [*ledger.rows(devices, 'devices'), {'id': 'other-box', 'hostname': 'aws-build', 'tags': ['tag:build-aws']}]
        ledger.rows(devices, 'devices')[0]['nodeId'] = 'node-confirmed'
        saved.update(tailscale_node_id='node-confirmed', tailscale_instance_id='i-fixture')
        _ = path.write_text(json.dumps(saved))
        success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertEqual([row['id'] for row in ledger.rows(devices, 'devices')], ['device-other', 'other-box'])

    def test_down_refuses_a_replaced_scaler_user_before_deleting_keys(self) -> None:
        devices = self.removal_fixture()
        self.assertIn('devices', devices)
        self.replace('scaler_user_name', 'another-user')
        override = self.root / 'get-user.json'
        _ = override.write_text(json.dumps({'User': {'Arn': 'arn:aws:iam::123456789012:user/build-scaler', 'UserId': 'recreated-user'}}))
        before = len(self.commands('delete-access-key'))
        success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('scaler ARN or UserId differs', output)
        self.assertEqual(len(self.commands('delete-access-key')), before)
        override.unlink()
        success, output = self.down_output()
        self.assertTrue(success, output)
        args = cast(list[str], self.commands('delete-access-key')[-1]['args'])
        self.assertEqual(args[args.index('--user-name') + 1], 'build-scaler')

    def test_down_refuses_stale_journals_and_foreign_retained_volume_tags(self) -> None:
        devices = self.removal_fixture()
        self.assertIn('devices', devices)
        saved = remote.read_record(self.state / 'deployment.json')
        journal = self.state / 'down.json'
        _ = journal.write_text(json.dumps({**saved, 'deployment_id': '0' * 32}))
        success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('removal journal differs', output)
        self.assertTrue((self.root / 'controller-enabled').exists())
        journal.unlink()
        override = self.root / 'describe-volumes.json'
        _ = override.write_text(json.dumps({'Volumes': [{'VolumeId': 'vol-created', 'State': 'available', 'Attachments': [],
                                                         'Tags': [{'Key': 'DeploymentId', 'Value': 'another-deployment'},
                                                                  {'Key': 'aws:cloudformation:stack-id', 'Value': saved['stack_id']}]}]}))
        success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('deployment ownership tag', output)
        self.assertEqual(self.commands('delete-volume'), [])
        override.unlink()
        success, output = self.down_output()
        self.assertTrue(success, output)
        args = cast(list[str], self.commands('delete-budget')[-1]['args'])
        self.assertEqual(args[args.index('--budget-name') + 1], 'fixture-build-monthly-' + str(saved['deployment_id']))


    def test_review_in_progress_stack_has_no_resources_to_reconcile(self) -> None:
        _ = (self.root / 'review-stack').write_text('empty change set')
        _ = self.update_fixture()
        state = remote.read_record(self.root / 'fake-stack.json')
        outputs = ledger.mapping(state['outputs'])
        outputs.update(InstanceId='i-fixture', DataVolumeId='vol-created', ComputeEnabled='true')
        state['instance_state'] = 'stopped'
        write_text_atomic(self.root / 'fake-stack.json', json.dumps(state))
        saved = deployment_record = remote.read_record(self.state / 'deployment.json')
        saved.update(outputs={**outputs, 'DataVolumeId': ''}, data_volume_id='vol-created')
        write_text_atomic(self.state / 'deployment.json', json.dumps(saved))
        _ = (self.root / 'volume-owner').write_text(str(deployment_record['deployment_id']))
        write_text_atomic(self.state / 'up.json', json.dumps({'box-join': {}}))
        ledger.CostLedger(self.state).observe(ledger.InstanceObservation(self.now, 'terminated', 'historical builder fixture'), confirms_transition=True)
        self.assertEqual(len(self.commands('deploy')), 2)


    def test_prepare_refuses_missing_or_unreadable_notification_files(self) -> None:
        self.notification_file.unlink()
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                if unreadable:
                    self.notification_file.mkdir()
                with self.assertRaises((deploy.DeploymentRefused, ValueError)) as failure:
                    _ = self.deployment().prepare()
                self.assertIn('notification_email_file', str(failure.exception))
                self.assertIn(str(self.notification_file), str(failure.exception))

    def test_prepare_refuses_invalid_notification_content_without_printing_it(self) -> None:
        for content in ('', ' \n', 'build example@example.invalid', 'build.example.invalid',
                        'build@example.invalid\nother@example.invalid\n'):
            with self.subTest(content=content):
                _ = self.notification_file.write_text(content)
                with self.assertRaises((deploy.DeploymentRefused, ValueError)) as failure:
                    _ = self.deployment().prepare()
                message = str(failure.exception)
                self.assertIn('notification_email_file', message)
                self.assertIn(str(self.notification_file), message)
                if content.strip():
                    self.assertNotIn(content.strip(), message)

    def test_prepare_refuses_notification_paths_inside_the_public_repository(self) -> None:
        tracked_path = Path(__file__).resolve()
        self.notification_file.unlink()
        self.notification_file.symlink_to(tracked_path)
        for path in (tracked_path, self.notification_file):
            with self.subTest(path=path):
                self.replace('notification_email_file', str(path))
                with self.assertRaises((deploy.DeploymentRefused, ValueError)) as failure:
                    _ = self.deployment().prepare()
                self.assertIn('notification_email_file', str(failure.exception))
                self.assertIn(str(path.resolve()), str(failure.exception))

    def test_prepare_uses_the_private_notification_address_in_saved_parameters(self) -> None:
        self.replace('notification_email_file', '~/notification-email')
        _ = self.notification_file.write_text('  build@example.invalid\n')
        prepared = self.deployment().prepare()
        self.assertEqual(prepared.parameters['NotificationEmail'], 'build@example.invalid')
        saved = cast(list[dict[str, str]], json.loads((self.bundle / 'parameters.json').read_text()))
        self.assertEqual([row['ParameterValue'] for row in saved if row['ParameterKey'] == 'NotificationEmail'],
                         ['build@example.invalid'])
        _ = self.notification_file.write_text('other@example.invalid\n')
        updated = self.deployment().prepare()
        self.assertEqual(updated.parameters['NotificationEmail'], 'other@example.invalid')
        self.assertNotEqual(updated.fingerprint, prepared.fingerprint)

    def test_apply_refuses_when_the_private_notification_file_disappears_after_prepare(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        self.notification_file.unlink()
        with self.assertRaises((deploy.DeploymentRefused, ValueError)) as failure:
            _ = subject.apply(approve_resources=True)
        self.assertIn('notification_email_file', str(failure.exception))
        self.assertIn(str(self.notification_file), str(failure.exception))
        self.assertEqual(self.commands('deploy'), [])


    def test_a_live_limit_change_after_preparation_refuses_every_resource(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        self.replace('daily_limit_usd', '1')
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(self.commands(), [])

    def test_a_limit_change_during_the_foundation_deploy_refuses_the_next_change(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        execute = subject.execute

        def change_limit(arguments: Sequence[str], *, capture_output: bool, text: bool,
                         timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            result = execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
            if 'deploy' in arguments:
                self.replace('monthly_limit_usd', '1')
            return result

        subject.execute = change_limit
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.apply(approve_resources=True)
        calls = self.commands('deploy')
        self.assertEqual(len(calls), 1)

    def test_apply_requires_explicit_user_gate_and_private_credentials(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.apply()
        self.credentials.chmod(0o644)
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(self.commands(), [])


    def test_prepare_preserves_existing_cost_and_claim_evidence(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        book = ledger.CostLedger(self.state)
        book.observe(ledger.InstanceObservation(self.now, 'stopped', 'fixture'))
        self.set_clock(self.now + 1)
        _ = subject.prepare()
        self.assertEqual(book.read_snapshot().observations, [ledger.InstanceObservation(self.now - 1, 'stopped', 'fixture')])


    def test_template_enforces_budget_retention_and_one_time_gate_boxes(self) -> None:
        template = remote.read_record(deploy.TEMPLATE)
        resources = cast(dict[str, dict[str, object]], template['Resources'])
        for name in ('InstanceRole', 'InstanceProfile', 'SecurityGroup', 'LaunchTemplate', 'Instance', 'DataVolume',
                     'DataAttachment', 'ScalerPolicy', 'StopRole', 'BudgetStop', 'StopPolicy'):
            with self.subTest(retired_builder_resource=name):
                self.assertNotIn(name, resources)
        budget_resource = resources['MonthlyBudget']
        self.assertEqual(budget_resource['DeletionPolicy'], 'Retain')
        self.assertEqual(budget_resource['UpdateReplacePolicy'], 'Retain')
        budget = cast(dict[str, object], budget_resource['Properties'])
        budget_data = cast(dict[str, object], budget['Budget'])
        self.assertEqual(budget_data['BudgetLimit'], {'Amount': {'Ref': 'MonthlyLimitUsd'}, 'Unit': 'USD'})
        launch = cast(dict[str, object], resources['GateLaunchTemplate']['Properties'])
        data = cast(dict[str, object], launch['LaunchTemplateData'])
        self.assertEqual(data['InstanceMarketOptions'], {
            'MarketType': 'spot', 'SpotOptions': {'SpotInstanceType': 'one-time',
            'InstanceInterruptionBehavior': 'terminate', 'MaxPrice': {'Ref': 'GateSpotMaxPrice'}}})
        self.assertEqual(data['InstanceInitiatedShutdownBehavior'], 'terminate')
        mappings = cast(list[dict[str, object]], data['BlockDeviceMappings'])
        self.assertIs(cast(dict[str, object], mappings[0]['Ebs'])['DeleteOnTermination'], True)
        self.assertEqual(cast(dict[str, object], data['MetadataOptions'])['HttpTokens'], 'required')
        self.assertNotIn('AccessKey', json.dumps(template))
        self.assertNotIn('mkfs', json.dumps(template))


    def refuse_command(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                       timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.assertIsNone(env)
        _ = (capture_output, text, timeout, check)
        raise AssertionError(f'disabled tick called AWS: {arguments}')


    def test_compute_update_revalidates_the_selected_image(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.replace('ami_id', 'ami-unverified')
        _ = subject.prepare()
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'image identity'):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(len(self.commands('describe-images')), 2)
        self.assertEqual(len(self.commands('deploy')), 2)


    def fake_commands(self, source: str, *names: str) -> None:
        for name in names:
            path = self.root / 'bin' / name
            _ = path.write_text(source)
            path.chmod(0o700)

    def run_bash(self, path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(['bash', str(path)], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)

    def patch_source(self, path: str) -> str:
        if not self.patch_sections:
            patch = (deploy.TEMPLATE.parent / 'nixos.patch').read_text()
            for section in patch.split('diff --git ')[1:]:
                name = section.splitlines()[0].split(' b/', 1)[1]
                self.patch_sections[name] = '\n'.join(line[1:] for line in section.splitlines()
                                                     if line.startswith('+') and not line.startswith('+++')) + '\n'
        return self.patch_sections[path]

    def install_reviewed_template(self) -> None:
        hosts = self.root / 'nixos/hosts'
        _ = (hosts / 'aws-build-base.nix').write_text(self.patch_source('hosts/aws-build-base.nix'))

    def reviewed_bundle(self) -> str:
        self.install_reviewed_template()
        with mock.patch.object(sys, 'argv', ['deploy.py', 'bootstrap', '--settings', str(self.settings),
                                           '--output', str(self.bundle)]):
            self.assertEqual(deploy.main(), 0)
        return (self.bundle / 'bootstrap.sh').read_text()


    def install_volume_fakes(self) -> None:
        state = self.root / 'volume-state'
        state.mkdir()
        _ = (state / 'data-volume-id').write_text('vol-0123456789abcdef0\n')
        (self.root / 'device').touch()
        fake = r'''#!/bin/sh
set -eu
root=$DEPLOY_FIXTURE
case ${0##*/} in
  udevadm) test "$1" = wait ;;
  readlink)
    test "$1" = -f
    test "$2" = /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0123456789abcdef0
    printf '%s/device\n' "$root" ;;
  lsblk)
    case "$3" in
      SERIAL) printf '%s\n' "$(cat "$root/serial")" ;;
      TYPE) printf 'disk\n' ;;
      *) test "$3" = MOUNTPOINTS ;;
    esac ;;
  blkid)
    test "$1" = -s
    test "$3" = -o
    test "$4" = value
    test "$5" = "$root/device"
    if test -f "$root/TYPE" || test -f "$root/LABEL"; then
      if test -f "$root/$2"; then cat "$root/$2"; else printf '\n'; fi
    else
      printf '\n'
      exit 2
    fi ;;
  wipefs)
    if test -f "$root/TYPE"; then cat "$root/TYPE"; else printf '\n'; fi ;;
  mkfs.ext4)
    test "$#" -eq 3
    test "$1" = -L
    test "$2" = aws-home
    test "$3" = "$root/device"
    printf 'format\n' >> "$root/formats"
    printf 'ext4\n' > "$root/TYPE"
    printf 'aws-home\n' > "$root/LABEL" ;;
  *) exit 1 ;;
esac
'''
        self.fake_commands(fake, 'udevadm', 'readlink', 'lsblk', 'blkid', 'wipefs', 'mkfs.ext4')

    def volume_script(self, *, bootstrap: bool, existing: str = '') -> Path:
        base = self.patch_source('hosts/aws-build-base.nix')
        host = self.patch_source('hosts/aws-build.nix')
        self.assertRegex(host, r'imports\s*=\s*\[\s*\(import\s+\./aws-build-base\.nix\b')
        self.assertIn('existingVolume = if bootstrap then installation.existingVolume else "";', base)
        if bootstrap:
            self.replace('existing_data_volume_id', existing)
            source = self.reviewed_bundle()
            installation = source.split('    installation = {', 1)[1].split('    };', 1)[0]
            volume = re.search(r'existingVolume = ("[^"\n]*");', installation)
            delay = re.search(r'volumeWaitSeconds = ([0-9]+);', installation)
            if volume is None or delay is None:
                self.fail('the rendered installation needs volume settings')
            existing = cast(str, json.loads(volume.group(1)))
            wait = '--timeout=' + delay.group(1)
        else:
            source, existing, wait = base, '', ''
        volume_script = source.split('systemd.services.aws-build-volume = {', 1)[1]
        script = volume_script.split("script = ''\n", 1)[1].split("\n    '';", 1)[0]
        bindings = {
            '${pkgs.systemd}/bin/udevadm': 'udevadm',
            '${lib.optionalString bootstrap "--timeout=${toString installation.volumeWaitSeconds}"}': wait,
            '${if bootstrap then "false" else "true"}': 'false' if bootstrap else 'true',
            '${lib.escapeShellArg existingVolume}': shlex.quote(existing),
            '/var/lib/aws-build': str(self.root / 'volume-state'),
            'test -b "$device"': 'test -f "$device"',
        }
        self.assertEqual(script.count('test -b "$device"'), 1)
        for name, value in bindings.items():
            script = script.replace(name, value)
        self.assertNotIn('${', script)
        path = self.root / ('bootstrap-volume.sh' if bootstrap else 'host-volume.sh')
        _ = path.write_text(script)
        return path


    def test_earlier_day_request_costs_do_not_reduce_the_next_days_reconciled_guard_totals(self) -> None:
        self.text_setting('accounting_zone', 'America/New_York')
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0',
                    'observation_max_age_seconds': '259200', 'limit_reserve_usd': '0', 'daily_limit_usd': '18'})
        start = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp())
        end = start + 86400
        self.set_clock(end + 3600)
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'stopped', 'confirmed stopped'))
        book.reconcile(ledger.CostReconciliation(self.now, start, end, Decimal(24), 'UTC bill'))
        before = book.guard(self.settings, 'deployment', self.now, lambda _event: True)
        before_costs = narrow(self, before.evidence.costs, ledger.CostEstimate)
        self.assertEqual(before_costs.daily.usd, Decimal(19))
        self.assertEqual(before_costs.monthly.usd, Decimal(19))
        self.assertIsInstance(before, ledger.SpendingRefused)
        request = ledger.ComputeRequest(start, 'replacement', 'us-east-1', Decimal(2))
        book.record_compute_request(request)
        book.resolve_compute_request(request, ledger.ComputeResourcesAbsent(start + 5 * 3600))
        after = book.guard(self.settings, 'deployment', self.now, lambda _event: True)
        after_costs = narrow(self, after.evidence.costs, ledger.CostEstimate)
        self.assertGreaterEqual(after_costs.daily.usd, before_costs.daily.usd)
        self.assertGreaterEqual(after_costs.monthly.usd, before_costs.monthly.usd)
        self.assertIsInstance(after, ledger.SpendingRefused)
        self.assertEqual(sum(row.confirmed_uptime_s for row in after_costs.intervals), 0)

    def test_lower_price_keeps_each_established_day_and_the_guard_refusal(self) -> None:
        self.text_setting('accounting_zone', 'America/New_York')
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0',
                    'observation_max_age_seconds': '259200', 'price_max_age_seconds': '259200',
                    'fallback_usd_per_hour': '1', 'limit_reserve_usd': '0', 'daily_limit_usd': '20'})
        start = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp())
        end = start + 86400
        self.set_clock(end)
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'running', 'confirmed running'))
        book.observe(ledger.InstanceObservation(end, 'stopped', 'confirmed stopped'))
        book.record_price(ledger.PriceSample(start, Decimal('0.1'), 'earlier day price'))
        book.record_price(ledger.PriceSample(start + 5 * 3600, Decimal(1), 'later day price'))
        book.reconcile(ledger.CostReconciliation(self.now, start, end, Decimal(24), 'Cost Explorer'))
        before = book.guard(self.settings, 'deployment', self.now, lambda _event: True)
        self.assertIsInstance(before, ledger.SpendingRefused)
        before_costs = narrow(self, before.evidence.costs, ledger.CostEstimate)
        book.record_price(ledger.PriceSample(start + 6 * 3600, Decimal('0.1'), 'lower later price'))
        reopened = ledger.CostLedger(self.state)
        after = reopened.guard(self.settings, 'deployment', self.now, lambda _event: True)
        after_costs = narrow(self, after.evidence.costs, ledger.CostEstimate)
        self.assertGreaterEqual(after_costs.daily.usd, before_costs.daily.usd)
        self.assertGreaterEqual(after_costs.monthly.usd, before_costs.monthly.usd)
        self.assertIsInstance(after, ledger.SpendingRefused)
        saved = reopened.path.read_bytes()
        snapshot = reopened.read_snapshot()
        readonly = ledger.costs_from_evidence(snapshot, aws_config.read_settings(self.settings), self.now)
        self.assertEqual(reopened.path.read_bytes(), saved)
        self.assertEqual(snapshot, reopened.read_snapshot())
        self.assertEqual(readonly.daily, after_costs.daily)

        charges = self.state / 'charges'
        charges.mkdir()
        reading = ledger.ChargeRead('2026-02-01', self.now + 1, start, end, Decimal(24), Decimal(0), Decimal(0))
        _ = (charges / '2026-02-01.json').write_text(json.dumps(ledger.charge_record(reading)))
        recovered = reopened.refresh_charges(self.settings, self.now + 1,
                                             lambda _start, _end: self.fail('recover the saved reply first'))
        self.assertEqual(recovered, reading)
        resolved = reopened.guard(self.settings, 'deployment', self.now + 1, lambda _event: True)
        resolved_costs = narrow(self, resolved.evidence.costs, ledger.CostEstimate)
        self.assertGreaterEqual(resolved_costs.daily.usd, before_costs.daily.usd)
        self.assertGreaterEqual(resolved_costs.monthly.usd, before_costs.monthly.usd)
        self.assertIsInstance(resolved, ledger.SpendingRefused)

    def test_matching_bill_adds_no_reconciliation_charge(self) -> None:
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0',
                    'observation_max_age_seconds': '259200', 'price_max_age_seconds': '259200'})
        start = self.now - 86400
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'running', 'confirmed running'))
        book.record_price(ledger.PriceSample(start, Decimal(1), 'confirmed price'))
        book.reconcile(ledger.CostReconciliation(self.now, start, self.now, Decimal(24), 'UTC bill'))
        costs = book.estimate(aws_config.read_settings(self.settings), self.now)
        self.assertEqual(ledger.period_cost(costs.intervals, start, self.now), Decimal(24))
        self.assertEqual(sum(r.usd for r in costs.intervals if r.kind == 'reconciliation'), Decimal(0))

    def test_readonly_measurements_leave_reconciled_day_totals_unestablished(self) -> None:
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0'})
        start = self.now - 3600
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'stopped', 'confirmed stopped'))
        book.observe(ledger.InstanceObservation(self.now, 'stopped', 'fresh confirmed stopped'))
        book.reconcile(ledger.CostReconciliation(self.now, start, self.now, Decimal(2), 'UTC bill'))
        saved = book.path.read_bytes()
        snapshot = book.read_snapshot()
        costs = ledger.costs_from_evidence(snapshot, aws_config.read_settings(self.settings), self.now)
        self.assertEqual(costs.daily.usd, Decimal(2))
        self.assertEqual(snapshot.reconciliations[0].established_days, ())
        self.assertEqual(book.path.read_bytes(), saved)
        _ = book.estimate(aws_config.read_settings(self.settings), self.now)
        reopened = ledger.CostLedger(self.state).read_snapshot()
        self.assertEqual(reopened.reconciliations[0].established_days,
                         (ledger.EstablishedDayCost(DAY, Decimal(2)),))

    def test_estimate_preserves_distinct_replies_for_the_same_period(self) -> None:
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0'})
        start = self.now - 60
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'stopped', 'confirmed stopped'))
        readings = (ledger.CostReconciliation(self.now, start, self.now, Decimal(2), 'UTC bill'),
                    ledger.CostReconciliation(self.now, start, self.now, Decimal(1), 'UTC bill'))
        for reading in readings:
            book.reconcile(reading)
        costs = book.estimate(aws_config.read_settings(self.settings), self.now)
        self.assertEqual(costs.daily.usd, Decimal(2))
        self.assertEqual(tuple(ledger.CostLedger(self.state).read_snapshot().reconciliations), readings)


    def test_budget_stop_percent_uses_its_source_limit(self) -> None:
        self.replace('budget_stop_percent', '99')
        with self.assertRaises(ValueError):
            _ = aws_config.read_deployment_settings(self.settings)

    def test_durable_decision_check_rejects_a_superseded_decision(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        book = ledger.CostLedger(self.state)
        allowed = ledger.spending_guard(self.settings, self.state, 'deployment', clock=lambda: self.now)
        self.assertTrue(book.decision_is_current(allowed))
        self.replace('daily_limit_usd', '1')
        _ = ledger.spending_guard(self.settings, self.state, 'deployment', clock=lambda: self.now, notify=lambda _event: True)
        self.assertFalse(book.decision_is_current(allowed))
        self.assertIsInstance(book.permission_for_decision(allowed), ledger.PermissionUnavailable)


    def test_prepare_reads_the_template_once_and_uses_its_guard_decision(self) -> None:
        original = Path.read_bytes
        reads: list[Path] = []
        def read(path: Path) -> bytes:
            if path == deploy.TEMPLATE:
                reads.append(path)
            return original(path)
        with mock.patch.object(Path, 'read_bytes', read):
            _ = self.deployment().prepare()
        self.assertEqual(reads, [deploy.TEMPLATE])
        self.assertEqual(ledger.mapping(remote.read_record(self.bundle / 'preflight.json')['decision'])['purpose'], 'deployment')

    def test_shared_aws_cli_rejects_a_malformed_stack_reply(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = (self.root / 'describe-stacks.json').write_text('[]')
        with self.assertRaisesRegex(aws_target.AwsUnavailable, 'invalid reply'):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(self.commands('deploy'), [])

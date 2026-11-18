"""Preview a policy update for a stopped, stack-owned Hana box."""
from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import cast, final
from uuid import uuid4

from scripts.aws_target import admin_secrets, ledger, plan_identity
from scripts.aws_target.aws_cli import AwsCli, AwsExecutor, AwsUnavailable, aws_error_code


@final
class ApprovedSetup:
    """Use the normal setup path after terminal approval."""


APPROVED_SETUP = ApprovedSetup()


class PlanRefused(ValueError):
    """The available evidence does not permit the setup plan."""


def unsupported(state: str) -> PlanRefused:
    return PlanRefused(f'no plan run exists for this state ({state})')


def safe_reason(error: Exception) -> str:
    message = admin_secrets.redact(str(error))
    message = re.sub(r'https?://\S+|[^\s<>;,]+@[^\s<>;,]+|\b[\w-]+-[a-f0-9]{8,}\b', '[redacted]', message)
    return next((line.strip() for line in message.splitlines() if line.strip()), 'Setup could not be confirmed.')


def action_name(service: str, operation: str) -> str:
    return ('s3' if service == 's3api' else service) + ':' + ''.join(word.capitalize() for word in operation.split('-'))


PLAN_READS: frozenset[str] = frozenset((*plan_identity.READ_ACTIONS, *plan_identity.STACK_READS, *plan_identity.IAM_READS,
                       *plan_identity.BUCKET_READS))
INLINE_TEMPLATE_LIMIT = 51_200
CHANGE_SET_ABSENT = ('ChangeSetNotFound', 'ChangeSetNotFoundException')
CHANGE_SET_FINISHED = ('CREATE_COMPLETE', 'FAILED', 'DELETE_COMPLETE', 'DELETE_FAILED')

# The installed CLI model predates PolicyExclusionList. Add its documented ARN variant.
IAM_POLICY_EXCLUSION_MODEL = {'merge': {'shapes': {
    'SimulatePrincipalPolicyRequest': {'members': {'PolicyExclusionList': {'shape': 'PolicyExclusionListType'}}},
    'PolicyExclusionListType': {'type': 'list', 'member': {'shape': 'PolicyIdentifier'}},
    'PolicyIdentifier': {'type': 'structure', 'members': {'PolicyArn': {'shape': 'arnType'}}},
}}}


@dataclass(frozen=True)
class ScalerPolicyReplacement:
    live_arn: str
    proposed_document: str


@dataclass(frozen=True)
class BucketPolicyKnown:
    document: str


@dataclass(frozen=True)
class BucketPolicyNotSimulated:
    pass


def resolve_template(value: object, refs: Mapping[str, str], attributes: Mapping[str, str]) -> object:
    """Resolve policy values from the existing stack evidence."""
    if isinstance(value, list):
        return [resolve_template(item, refs, attributes) for item in cast(list[object], value)]
    if not isinstance(value, dict):
        return value
    row = ledger.mapping(cast(object, value))
    if row.keys() == {'Ref'}:
        return refs[ledger.text(row, 'Ref')]
    if row.keys() == {'Fn::GetAtt'}:
        field = row['Fn::GetAtt']
        if isinstance(field, list) and len(cast(list[object], field)) == 2:
            logical_id, attribute = cast(list[object], field)
            if isinstance(logical_id, str) and isinstance(attribute, str):
                return attributes[f'{logical_id}.{attribute}']
        if isinstance(field, str):
            return attributes[field]
        raise ValueError('The template has an invalid Fn::GetAtt.')
    if row.keys() == {'Fn::Sub'}:
        values = dict(refs) | dict(attributes)
        field = row['Fn::Sub']
        if isinstance(field, list) and len(cast(list[object], field)) == 2:
            field, bindings = cast(list[object], field)
            for name, binding in ledger.mapping(bindings).items():
                resolved = resolve_template(binding, refs, attributes)
                if not isinstance(resolved, str):
                    raise ValueError('The template has a non-text Fn::Sub value.')
                values[name] = resolved
        if not isinstance(field, str):
            raise ValueError('The template has an invalid Fn::Sub.')
        return re.sub(r'\$\{([^}]+)\}', lambda match: values[match[1]], field)
    if any(key.startswith('Fn::') or key == 'Ref' for key in row):
        raise ValueError('The policy uses an unsupported template expression.')
    return {key: resolve_template(item, refs, attributes) for key, item in row.items()}


@final
class PlanRun:
    def __init__(self, load_credential: Callable[[], plan_identity.PlanCredentials], executor: AwsExecutor,
                 pause: Callable[[float], None]) -> None:
        self.load_credential = load_credential
        self.execute = executor
        self.pause = pause

    @cached_property
    def credential(self) -> plan_identity.PlanCredentials:
        return self.load_credential()

    def api(self, region: str, timeout: float) -> AwsCli:
        return AwsCli('plan', region, timeout, self.read)

    def read(self, arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
             check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        _ = env
        service, operation = arguments[5:7]
        action = action_name(service, operation)
        if action not in PLAN_READS:
            raise PlanRefused(f'The plan identity cannot call {action}.')
        return self._execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check)

    def _execute(self, arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                 check: bool) -> subprocess.CompletedProcess[str]:
        command = [arguments[0], *arguments[3:]]
        service, operation = arguments[5:7]
        try:
            with self._environment(service, operation) as environment:
                result = self.execute(command, capture_output=capture_output, text=text, timeout=timeout, check=check,
                                      env=environment)
        except (OSError, subprocess.SubprocessError):
            raise PlanRefused(f'AWS did not confirm {operation} before the call ended.') from None
        if result.returncode:
            code = aws_error_code(AwsUnavailable(result.stderr))
            if code:
                api_name = action_name(service, operation).split(':')[1]
                message = f'An error occurred ({code}) when calling the {api_name} operation'
            else:
                message = f'AWS did not confirm {operation}.'
            return subprocess.CompletedProcess(command, result.returncode, '', message)
        return result

    @contextmanager
    def _environment(self, service: str, operation: str) -> Generator[dict[str, str]]:
        environment = self.credential.environment()
        if (service, operation) != ('iam', 'simulate-principal-policy'):
            yield environment
            return
        with tempfile.TemporaryDirectory(prefix='aws-plan-model-') as folder:
            model = Path(folder) / 'iam/2010-05-08/service-2.sdk-extras.json'
            model.parent.mkdir(parents=True)
            _ = model.write_text(json.dumps(IAM_POLICY_EXCLUSION_MODEL))
            yield environment | {'AWS_DATA_PATH': folder}

    def confirm_account(self, region: str, timeout: float) -> None:
        owner = self.credential.owner
        identity = self.api(region, timeout).call('get-caller-identity', service='sts')
        if identity.get('Account') != owner.account or identity.get('Arn') != owner.user_arn:
            raise PlanRefused('STS did not confirm the plan identity.')

    def scaler(self, api: AwsCli, name: str) -> str:
        identity = api.call('get-caller-identity', service='sts')
        owner = self.credential.owner
        arn = ledger.text(identity, 'Arn')
        if (identity.get('Account') != owner.account or arn == owner.user_arn
                or not arn.startswith(owner.iam('user/')) or arn.rsplit('/', 1)[1] != name):
            raise PlanRefused('STS did not confirm the configured Hana scaler user.')
        return arn

    def scaler_policy(self, api: AwsCli, stack: str, proposed: object, parameters: dict[str, str],
                      bucket: str) -> ScalerPolicyReplacement:
        reply = api.call('describe-stack-resource', '--stack-name', stack, '--logical-resource-id', 'ScalerPolicy',
                         service='cloudformation')
        resource = ledger.mapping(reply['StackResourceDetail'])
        arn = ledger.text(resource, 'PhysicalResourceId')
        owner = self.credential.owner
        if (resource.get('ResourceType') != 'AWS::IAM::ManagedPolicy'
                or resource.get('LogicalResourceId') != 'ScalerPolicy'
                or not arn.startswith(owner.iam('policy/'))
                or not re.fullmatch(r'[\w+=,.@/-]+', arn.removeprefix(owner.iam('policy/')), re.ASCII)):
            raise PlanRefused('CloudFormation did not identify the live Hana scaler policy.')
        properties = ledger.mapping(ledger.mapping(proposed)['Properties'])
        if properties.get('Users') != [{'Ref': 'ScalerUser'}]:
            raise PlanRefused('The proposed Hana scaler policy targets a different user.')
        document = resolve_template(properties['PolicyDocument'], self._template_refs(api.region, parameters),
                                    {'ControlBucket.Arn': self.s3(bucket)})
        return ScalerPolicyReplacement(arn, json.dumps(document, separators=(',', ':')))

    def _template_refs(self, region: str, parameters: dict[str, str]) -> dict[str, str]:
        owner = self.credential.owner
        return parameters | {'AWS::Partition': owner.partition, 'AWS::AccountId': owner.account, 'AWS::Region': region}

    def s3(self, path: str) -> str:
        return f'arn:{self.credential.owner.partition}:s3:::{path}'

    def bucket_policy(self, region: str, template: dict[str, object], parameters: dict[str, str],
                      bucket: str) -> BucketPolicyKnown | BucketPolicyNotSimulated:
        resources = ledger.mapping(template['Resources'])
        outputs = ledger.mapping(template['Outputs'])
        bucket_value = ledger.mapping(outputs.get('ControlBucket', {})).get('Value')
        refs = self._template_refs(region, parameters)
        attributes: dict[str, str] = {}
        if isinstance(bucket_value, dict):
            logical_id = ledger.mapping(cast(object, bucket_value)).get('Ref')
            if isinstance(logical_id, str) and ledger.mapping(resources.get(logical_id, {})).get('Type') == 'AWS::S3::Bucket':
                refs[logical_id] = bucket
                attributes[f'{logical_id}.Arn'] = self.s3(bucket)
        unresolved = False
        for logical_id, resource in resources.items():
            row = ledger.mapping(resource)
            if row.get('Type') != 'AWS::S3::BucketPolicy':
                continue
            try:
                properties = ledger.mapping(row['Properties'])
                if resolve_template(properties['Bucket'], refs, attributes) != bucket:
                    continue
                if 'Condition' in row:
                    raise ValueError('The bucket policy has a template condition.')
                document = ledger.mapping(resolve_template(properties['PolicyDocument'], refs, attributes))
            except (KeyError, ValueError):
                print(f'plan note: bucket policy not simulated ({logical_id})', flush=True)
                unresolved = True
                continue
            return BucketPolicyKnown(json.dumps(document, separators=(',', ':')))
        if not unresolved:
            print('plan note: no bucket policy in the stack template', flush=True)
        return BucketPolicyNotSimulated()

    def probe_scaler(self, api: AwsCli, principal: str, replacement: ScalerPolicyReplacement, instance: str,
                     deployment: str, bucket: str, bucket_policy: BucketPolicyKnown | BucketPolicyNotSimulated) -> None:
        def simulate(action: str, resource: str = '*', *, context: Mapping[str, str] | None = None,
                     policy: str = '') -> None:
            self._simulate(api, principal, replacement, action, resource, context or {}, policy)

        expected = {'HanaDeployment': deployment, 'Workload': 'hana'}
        reply = api.call('describe-instances', '--instance-ids', instance)
        rows = [row for group in ledger.rows(reply, 'Reservations') for row in ledger.rows(group, 'Instances')]
        if len(rows) != 1 or rows[0].get('InstanceId') != instance:
            raise PlanRefused('The live Hana scaler probe instance differs.')
        tags = {ledger.text(row, 'Key'): ledger.text(row, 'Value') for row in ledger.rows(rows[0], 'Tags')}
        if any(tags.get(key) != value for key, value in expected.items()):
            raise PlanRefused('The live Hana instance tags differ from the setup record.')
        print('plan guard: Hana instance tags passed', flush=True)
        simulate('ec2:DescribeInstances')
        for action in ('ec2:DescribeSpotInstanceRequests', 'ec2:DescribeInstanceAttribute'):
            print(f'plan Hana scaler: {action} simulation only', flush=True)
            simulate(action)
        context = {'ec2:ResourceTag/' + key: tags[key] for key in tags}
        owner = self.credential.owner
        resource = f'arn:{owner.partition}:ec2:{api.region}:{owner.account}:instance/{instance}'
        simulate('ec2:ModifyInstanceAttribute', resource, context=context | {'ec2:Attribute': 'userData'})
        for action in ('ec2:StartInstances', 'ec2:StopInstances'):
            simulate(action, resource, context=context)
        policy = bucket_policy.document if isinstance(bucket_policy, BucketPolicyKnown) else ''
        key = f'control/{deployment}/setup-probe.json'
        for action in ('s3:PutObject', 's3:GetObject'):
            simulate(action, self.s3(f'{bucket}/{key}'), policy=policy)

    @staticmethod
    def _simulate(api: AwsCli, principal: str, replacement: ScalerPolicyReplacement, action: str, resource: str,
                  context: Mapping[str, str], policy: str) -> None:
        entries = {'aws:RequestedRegion': api.region, 'aws:PrincipalArn': principal, **context}
        request: dict[str, object] = {
            'PolicySourceArn': principal,
            'PolicyExclusionList': [{'PolicyArn': replacement.live_arn}],
            'PolicyInputList': [replacement.proposed_document],
            'ActionNames': [action],
            'ResourceArns': [resource],
            'ContextEntries': [{'ContextKeyName': key, 'ContextKeyType': 'string', 'ContextKeyValues': [value]}
                               for key, value in entries.items()],
        }
        if action.startswith('s3:'):
            request['ContextEntries'] = [
                *cast(list[dict[str, object]], request['ContextEntries']),
                {'ContextKeyName': 'aws:SecureTransport', 'ContextKeyType': 'boolean', 'ContextKeyValues': ['true']},
            ]
        if policy:
            request['ResourcePolicy'] = policy
        reply = api.call('simulate-principal-policy', '--cli-input-json', json.dumps(request), '--no-paginate', service='iam')
        results = ledger.rows(reply, 'EvaluationResults')
        if reply.get('IsTruncated') or len(results) != 1:
            raise PlanRefused(f'Hana scaler {action} simulation is incomplete.')
        result = results[0]
        if result.get('EvalActionName') != action or result.get('MissingContextValues'):
            raise PlanRefused(f'Hana scaler {action} simulation is incomplete.')
        rows = ledger.rows(result, 'ResourceSpecificResults') if 'ResourceSpecificResults' in result else (result,)
        field = 'EvalResourceDecision' if 'ResourceSpecificResults' in result else 'EvalDecision'
        if len(rows) != 1 or rows[0].get('EvalResourceName') != resource or rows[0].get('MissingContextValues'):
            raise PlanRefused(f'Hana scaler {action} simulation is incomplete.')
        decision = rows[0].get(field)
        if decision not in ('allowed', 'explicitDeny', 'implicitDeny'):
            raise PlanRefused(f'Hana scaler {action} simulation is incomplete.')
        print(f'plan Hana scaler: {action} {"allowed" if decision == "allowed" else "denied"}', flush=True)
        if decision != 'allowed':
            raise PlanRefused(f'Hana scaler {action} denied.')

    @contextmanager
    def change(self, api: AwsCli, stack: str, template: bytes, values: list[dict[str, object]],
               timeout: int) -> Generator[dict[str, object]]:
        if len(template) > INLINE_TEMPLATE_LIMIT:
            raise PlanRefused('The template exceeds the 51,200-byte inline CloudFormation limit.')
        content = template.decode()
        name = plan_identity.CHANGE_SET_PREFIX + uuid4().hex
        target = ('--stack-name', stack, '--change-set-name', name)

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                    check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            _ = env
            if tuple(arguments[5:7]) not in (('cloudformation', 'create-change-set'),
                                              ('cloudformation', 'delete-change-set')):
                raise PlanRefused('This plan operation requires a change set request.')
            return self._execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check)

        writer = AwsCli('plan', api.region, api.timeout_s, execute)
        try:
            _ = writer.call('create-change-set', *target, '--change-set-type', 'UPDATE', '--template-body', content,
                            '--capabilities', 'CAPABILITY_NAMED_IAM', '--parameters', json.dumps(values),
                            service='cloudformation')
            deadline = time.monotonic() + timeout
            while True:
                change = api.call('describe-change-set', *target, '--no-paginate', service='cloudformation')
                if change.get('Status') in CHANGE_SET_FINISHED:
                    break
                if change.get('Status') not in ('CREATE_PENDING', 'CREATE_IN_PROGRESS'):
                    raise PlanRefused('CloudFormation returned an invalid change set status.')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PlanRefused('CloudFormation did not complete the plan change set before the deadline.')
                self.pause(min(2, remaining))
            print(f'plan stack: UPDATE in {api.region}', flush=True)
            yield change
        finally:
            self._remove_change_set(api, writer, stack, name)

    def _remove_change_set(self, api: AwsCli, writer: AwsCli, stack: str, name: str) -> None:
        target = ('--stack-name', stack, '--change-set-name', name)
        try:
            _ = writer.call('delete-change-set', *target, service='cloudformation')
        except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError):
            print(f'plan stack: delete call did not confirm removal of {name}', flush=True)
        for delay in plan_identity.PROOF_RETRY_DELAYS:
            if delay:
                self.pause(delay)
            try:
                _ = api.call('describe-change-set', *target, '--no-paginate', service='cloudformation')
            except AwsUnavailable as error:
                if aws_error_code(error) in CHANGE_SET_ABSENT:
                    print('plan stack: change set removal confirmed', flush=True)
                    return
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
        print(f'plan stack: change set may remain: {name}', flush=True)
        raise PlanRefused('change set not removed') from None

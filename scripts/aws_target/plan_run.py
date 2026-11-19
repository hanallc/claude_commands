"""Preview setup changes with the AWS plan identity."""
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


class GateStackAbsent(PlanRefused):
    """CloudFormation confirmed that the named gate stack does not exist."""


class _ChangeSetCleanupDeadline(Exception):
    """The time for change set cleanup has expired."""


def unsupported(state: str) -> PlanRefused:
    return PlanRefused(f'no plan run exists for this state ({state})')


def safe_reason(error: Exception) -> str:
    message = admin_secrets.redact(str(error))
    message = re.sub(r'https?://\S+|[^\s<>;,]+@[^\s<>;,]+|\b[\w-]+-[a-f0-9]{8,}\b', '[redacted]', message)
    return next((line.strip() for line in message.splitlines() if line.strip()), 'Setup could not be confirmed.')


def action_name(service: str, operation: str) -> str:
    namespace = {'s3api': 's3', 'service-quotas': 'servicequotas'}.get(service, service)
    return namespace + ':' + ''.join(word.capitalize() for word in operation.split('-'))


PLAN_READS: frozenset[str] = frozenset((*plan_identity.READ_ACTIONS, *plan_identity.STACK_READS, *plan_identity.IAM_READS,
                       *plan_identity.BUCKET_READS))
INLINE_TEMPLATE_LIMIT = 51_200
CHANGE_SET_ABSENT = ('ChangeSetNotFound', 'ChangeSetNotFoundException')
PLAN_ERRORS = (KeyError, OSError, TypeError, ValueError, AwsUnavailable, subprocess.SubprocessError)
CHANGE_SET_FINISHED = ('CREATE_COMPLETE', 'FAILED', 'DELETE_COMPLETE', 'DELETE_FAILED')
GATE_SCALER_POLICY_IDS = ('GateScalerPolicy', 'GateGpuTrialScalerPolicy')

# The installed CLI model predates PolicyExclusionList. Add its documented ARN variant.
IAM_POLICY_EXCLUSION_MODEL = {'merge': {'shapes': {
    'SimulatePrincipalPolicyRequest': {'members': {'PolicyExclusionList': {'shape': 'PolicyExclusionListType'}}},
    'PolicyExclusionListType': {'type': 'list', 'member': {'shape': 'PolicyIdentifier'}},
    'PolicyIdentifier': {'type': 'structure', 'members': {'PolicyArn': {'shape': 'arnType'}}},
}}}


@dataclass(frozen=True)
class ScalerPolicyPlan:
    live_arns: tuple[str, ...]
    proposed_documents: tuple[str, ...]


@dataclass(frozen=True)
class BucketPolicyKnown:
    document: str


@dataclass(frozen=True)
class BucketPolicyNotSimulated:
    pass


NO_TEMPLATE_VALUE = object()


def resolve_template(value: object, refs: Mapping[str, object], attributes: Mapping[str, str],
                     conditions: Mapping[str, bool] | None = None) -> object:
    """Resolve policy values from the existing stack evidence."""
    if isinstance(value, list):
        return [item for child in cast(list[object], value)
                if (item := resolve_template(child, refs, attributes, conditions)) is not NO_TEMPLATE_VALUE]
    if not isinstance(value, dict):
        return value
    row = ledger.mapping(cast(object, value))
    if row.keys() == {'Ref'}:
        name = ledger.text(row, 'Ref')
        return NO_TEMPLATE_VALUE if name == 'AWS::NoValue' else refs[name]
    if row.keys() == {'Fn::If'} and conditions is not None:
        condition, yes, no = cast(list[object], row['Fn::If'])
        if not isinstance(condition, str):
            raise ValueError('The template has an invalid Fn::If.')
        return resolve_template(yes if conditions[condition] else no, refs, attributes, conditions)
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
                resolved = resolve_template(binding, refs, attributes, conditions)
                if not isinstance(resolved, str):
                    raise ValueError('The template has a non-text Fn::Sub value.')
                values[name] = resolved
        if not isinstance(field, str):
            raise ValueError('The template has an invalid Fn::Sub.')
        def substitute(match: re.Match[str]) -> str:
            resolved = values[match[1]]
            if not isinstance(resolved, str):
                raise ValueError('The template has a non-text Fn::Sub value.')
            return resolved

        return re.sub(r'\$\{([^}]+)\}', substitute, field)
    if any(key.startswith('Fn::') or key == 'Ref' for key in row):
        raise ValueError('The policy uses an unsupported template expression.')
    return {key: resolve_template(item, refs, attributes, conditions) for key, item in row.items()}


@dataclass(frozen=True)
class GatePolicyResources:
    bucket: str
    profile: str
    role: str
    bucket_policy: str


@dataclass(frozen=True)
class GateUpdatePreview:
    summary: str
    displaced_policy_ids: tuple[str, ...]


@dataclass(frozen=True)
class GateDisplacedPolicies:
    stack_id: str
    logical_ids: tuple[str, ...]


@dataclass(frozen=True)
class GatePolicyExclusions:
    """The policy ARNs from the complete set of gate previews."""

    live_arns: tuple[str, ...]


@dataclass(frozen=True)
class GateLaunchProbe:
    deployment: str
    image: str
    vpc: str
    subnet: str
    template: str
    security_group: str
    instance_types: tuple[str, ...]
    request_tags: Mapping[str, str]
    artifact_key: str
    input_key: str


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
                if (code, service, operation) == ('ValidationError', 'cloudformation', 'describe-stacks'):
                    target = arguments[arguments.index('--stack-name') + 1]
                    if re.search(r': Stack with id ' + re.escape(target) + r' does not exist\s*$', result.stderr):
                        message += ': Stack does not exist'
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
                      bucket: str) -> ScalerPolicyPlan:
        arn = self.managed_policy_arn(api, stack, 'ScalerPolicy')
        properties = ledger.mapping(ledger.mapping(proposed)['Properties'])
        if properties.get('Users') != [{'Ref': 'ScalerUser'}]:
            raise PlanRefused('The proposed Hana scaler policy targets a different user.')
        document = resolve_template(properties['PolicyDocument'], self.template_refs(api.region, parameters),
                                    {'ControlBucket.Arn': self.s3(bucket)})
        return ScalerPolicyPlan((arn,), (json.dumps(document, separators=(',', ':')),))

    def managed_policy_arn(self, api: AwsCli, stack: str, logical_id: str) -> str:
        reply = api.call('describe-stack-resource', '--stack-name', stack, '--logical-resource-id', logical_id,
                         service='cloudformation')
        resource = ledger.mapping(reply['StackResourceDetail'])
        arn = ledger.text(resource, 'PhysicalResourceId')
        owner = self.credential.owner
        if (resource.get('ResourceType') != 'AWS::IAM::ManagedPolicy'
                or resource.get('LogicalResourceId') != logical_id
                or not arn.startswith(owner.iam('policy/'))
                or not re.fullmatch(r'[\w+=,.@/-]+', arn.removeprefix(owner.iam('policy/')), re.ASCII)):
            raise PlanRefused(f'CloudFormation did not identify the live {logical_id} policy.')
        return arn


    def template_refs(self, region: str, parameters: dict[str, str]) -> dict[str, str]:
        owner = self.credential.owner
        return parameters | {'AWS::Partition': owner.partition, 'AWS::AccountId': owner.account, 'AWS::Region': region}

    def s3(self, path: str) -> str:
        return f'arn:{self.credential.owner.partition}:s3:::{path}'

    def bucket_policy(self, region: str, template: dict[str, object], parameters: dict[str, str],
                      bucket: str) -> BucketPolicyKnown | BucketPolicyNotSimulated:
        resources = ledger.mapping(template['Resources'])
        outputs = ledger.mapping(template['Outputs'])
        bucket_value = ledger.mapping(outputs.get('ControlBucket', {})).get('Value')
        refs = self.template_refs(region, parameters)
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

    def probe_scaler(self, api: AwsCli, principal: str, replacement: ScalerPolicyPlan, instance: str,
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
        simulate('ec2:DescribeInstances')
        for action in ('ec2:DescribeSpotInstanceRequests', 'ec2:DescribeInstanceAttribute'):
            simulate(action)
            print(f'plan Hana scaler: {action} simulation only', flush=True)
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
        print('plan guard: Hana instance tags passed', flush=True)

    def probe_hana_replacement(self, api: AwsCli, principal: str, policy: ScalerPolicyPlan) -> None:
        """Check each resource in the proposed root replacement grant."""
        for action in ('ec2:DescribeImages', 'ec2:DescribeVolumes', 'ec2:DescribeReplaceRootVolumeTasks'):
            self._simulate(api, principal, policy, action, '*', {}, '')
        document = ledger.mapping(cast(object, json.loads(policy.proposed_documents[0])))
        action = 'ec2:CreateReplaceRootVolumeTask'
        for statement in ledger.rows(document, 'Statement'):
            if statement['Action'] != action:
                continue
            condition = ledger.mapping(statement.get('Condition', {}))
            values = ledger.mapping(condition.get('StringEquals', {}))
            context = {key: ledger.text(values, key) for key in values}
            scope = statement['Resource']
            resources = cast(list[str], scope) if isinstance(scope, list) else [ledger.text(statement, 'Resource')]
            for resource in resources:
                # The task and replacement volume IDs exist only after creation.
                resource = resource.removesuffix('*') + '00000000000000000' if resource.endswith('*') else resource
                self._simulate(api, principal, policy, action, resource, context, '')

    @staticmethod
    def _simulate(api: AwsCli, principal: str, replacement: ScalerPolicyPlan, action: str, resource: str,
                  context: Mapping[str, str], policy: str, *, label: str = 'Hana', show: bool = True) -> None:
        entries = {'aws:RequestedRegion': api.region, 'aws:PrincipalArn': principal, **context}
        request: dict[str, object] = {
            'PolicySourceArn': principal,
            'PolicyInputList': list(replacement.proposed_documents),
            'ActionNames': [action],
            'ResourceArns': [resource],
            'ContextEntries': [{'ContextKeyName': key, 'ContextKeyValues': [value], 'ContextKeyType': 'string'}
                               for key, value in entries.items()],
        }
        if replacement.live_arns:
            request['PolicyExclusionList'] = [{'PolicyArn': arn} for arn in replacement.live_arns]
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
            raise PlanRefused(f'{label} scaler {action} simulation is incomplete.')
        result = results[0]
        if result.get('EvalActionName') != action or result.get('MissingContextValues'):
            raise PlanRefused(f'{label} scaler {action} simulation is incomplete.')
        rows = ledger.rows(result, 'ResourceSpecificResults') if 'ResourceSpecificResults' in result else (result,)
        field = 'EvalResourceDecision' if 'ResourceSpecificResults' in result else 'EvalDecision'
        if len(rows) != 1 or rows[0].get('EvalResourceName') != resource or rows[0].get('MissingContextValues'):
            raise PlanRefused(f'{label} scaler {action} simulation is incomplete.')
        decision = rows[0].get(field)
        if decision not in ('allowed', 'explicitDeny', 'implicitDeny'):
            raise PlanRefused(f'{label} scaler {action} simulation is incomplete.')
        if decision != 'allowed':
            raise PlanRefused(f'{label} scaler {action} denied.')
        if show:
            print(f'plan {label} scaler: {action} allowed', flush=True)

    def gate_policy_documents(self, region: str, principal_name: str, template: dict[str, object],
                              parameters: dict[str, str]) -> tuple[tuple[str, ...], GatePolicyResources]:
        owner = self.credential.owner
        policies = ledger.mapping(template['Resources'])
        refs: dict[str, object] = {**self.template_refs(region, parameters), 'ScalerUser': principal_name}

        def resource_name(logical_id: str, property_name: str) -> str:
            properties = ledger.mapping(ledger.mapping(policies[logical_id])['Properties'])
            value = resolve_template(properties[property_name], refs, {})
            if not isinstance(value, str) or not value:
                raise PlanRefused(f'The proposed {logical_id} has no fixed name.')
            return value

        bucket = resource_name('GateBucket', 'BucketName')
        profile = owner.iam('instance-profile/' + resource_name('GateProfile', 'InstanceProfileName'))
        role = owner.iam('role/' + resource_name('GateRole', 'RoleName'))
        refs['GateBucket'] = bucket
        for key, definition in ledger.mapping(template['Parameters']).items():
            if ledger.mapping(definition).get('Type') == 'CommaDelimitedList':
                refs[key] = [item.strip() for item in parameters.get(key, '').split(',') if item.strip()]
        attributes = {'GateBucket.Arn': self.s3(bucket), 'GateProfile.Arn': profile, 'GateRole.Arn': role}
        conditions = {'GateHasRegionImages': bool(refs['GateRegionImages'])}
        documents: list[str] = []
        for logical_id in GATE_SCALER_POLICY_IDS:
            row = ledger.mapping(policies[logical_id])
            properties = ledger.mapping(row['Properties'])
            if (row.get('Type') != 'AWS::IAM::ManagedPolicy' or row.get('Condition') != 'Gates'
                    or properties.get('Users') != [{'Ref': 'ScalerUser'}]):
                raise PlanRefused('The proposed gate policies do not match the gate scaler.')
            document = ledger.mapping(resolve_template(properties['PolicyDocument'], refs, attributes, conditions))
            documents.append(json.dumps(document, separators=(',', ':')))
        policy = ledger.mapping(policies['GateBucketPolicy'])
        properties = ledger.mapping(policy['Properties'])
        if resolve_template(properties['Bucket'], refs, attributes) != bucket:
            raise PlanRefused('The proposed gate bucket policy targets a different bucket.')
        document = ledger.mapping(resolve_template(properties['PolicyDocument'], refs, attributes, conditions))
        return (tuple(documents),
                GatePolicyResources(bucket, profile, role,
                                    json.dumps(document, separators=(',', ':'))))

    def probe_gate_scaler(self, api: AwsCli, principal: str, exclusions: GatePolicyExclusions,
                          documents: tuple[str, ...], shared: GatePolicyResources, launch: GateLaunchProbe,
                          *, home: bool) -> None:
        owner = self.credential.owner
        policy = ScalerPolicyPlan(exclusions.live_arns, documents)

        def simulate(action: str, resource: str = '*', *, context: Mapping[str, str] | None = None) -> None:
            self._simulate(api, principal, policy, action, resource, context or {},
                           shared.bucket_policy if action.startswith('s3:') else '', label='Gate', show=False)

        def ec2(resource: str) -> str:
            return f'arn:{owner.partition}:ec2:{api.region}:{owner.account}:{resource}'

        if home:
            for action in ('ec2:DescribeInstances', 'ec2:DescribeInstanceTypeOfferings', 'ec2:DescribeSpotPriceHistory',
                           'servicequotas:GetServiceQuota', 'ce:GetCostAndUsage'):
                simulate(action)
        simulate('ec2:GetSpotPlacementScores', context={'ec2:Region': api.region})
        instance = ec2('instance/i-00000000000000000')
        volume = ec2('volume/vol-00000000000000000')
        resource_tags = {'GateDeployment': launch.deployment, 'Workload': 'cloud-claude-gate'}
        template = ec2('launch-template/' + launch.template)
        group = ec2('security-group/' + launch.security_group)
        subnet = ec2('subnet/' + launch.subnet)
        image = f'arn:{owner.partition}:ec2:{api.region}::image/{launch.image}'
        request_tags = {'aws:RequestTag/' + key: value for key, value in
                        {**resource_tags, **launch.request_tags}.items()}
        for size in launch.instance_types:
            launch_context = {'ec2:InstanceType': size, 'ec2:InstanceMarketType': 'spot',
                              'ec2:LaunchTemplate': template, 'ec2:InstanceProfile': shared.profile}
            simulate('ec2:RunInstances', image, context=launch_context)
            tagged = {'aws:ResourceTag/' + key: value for key, value in resource_tags.items()}
            for resource in (template, group):
                simulate('ec2:RunInstances', resource, context=launch_context | tagged)
            simulate('ec2:RunInstances', subnet, context=launch_context | {'ec2:Vpc': ec2('vpc/' + launch.vpc)})
            simulate('ec2:RunInstances', ec2('network-interface/eni-00000000000000000'), context=launch_context)
            for resource in (instance, volume):
                simulate('ec2:RunInstances', resource, context=launch_context | request_tags)
                simulate('ec2:CreateTags', resource, context=request_tags | {'ec2:CreateAction': 'RunInstances'})
        simulate('iam:PassRole', shared.role, context={'iam:PassedToService': 'ec2.amazonaws.com'})
        simulate('ec2:TerminateInstances', instance, context={
            'ec2:ResourceTag/' + key: value for key, value in resource_tags.items()})
        if home:
            for key in (launch.artifact_key, launch.input_key):
                for action in ('s3:PutObject', 's3:GetObject'):
                    simulate(action, self.s3(f'{shared.bucket}/{key}'))
            simulate('s3:ListBucket', self.s3(shared.bucket), context={'s3:prefix': launch.artifact_key})
            simulate('s3:AbortMultipartUpload', self.s3(f'{shared.bucket}/{launch.input_key}'))

    def gate_stack(self, api: AwsCli, name: str) -> dict[str, object]:
        try:
            reply = api.call('describe-stacks', '--stack-name', name, service='cloudformation')
        except AwsUnavailable as error:
            if aws_error_code(error) == 'ValidationError' and str(error).endswith(': Stack does not exist'):
                raise GateStackAbsent(
                    'no plan run exists for this state (the gate stack is absent; its CREATE preview can leave a review stack)'
                ) from None
            raise
        stacks = ledger.rows(reply, 'Stacks')
        owner = self.credential.owner
        prefix = f'arn:{owner.partition}:cloudformation:{api.region}:{owner.account}:stack/{name}/'
        if (len(stacks) != 1 or stacks[0].get('StackName') != name
                or not ledger.text(stacks[0], 'StackId').startswith(prefix)):
            raise PlanRefused('CloudFormation did not confirm the gate stack identity.')
        return stacks[0]

    @contextmanager
    def gate_update(self, api: AwsCli, stack: str, template: bytes, values: list[dict[str, object]],
                    timeout: int) -> Generator[GateUpdatePreview]:
        with self.change(api, stack, template, values, timeout) as change:
            reason = str(change.get('StatusReason', '')).lower()
            empty = change.get('Status') == 'FAILED' and any(text in reason for text in (
                "didn't contain changes", 'no updates are to be performed', 'no changes'))
            if change.get('NextToken'):
                raise PlanRefused('The gate change set has more than one page.')
            changes = ledger.rows({'Changes': change.get('Changes', [])}, 'Changes')
            if empty and not changes:
                yield GateUpdatePreview('UPDATE has no changes; scaler permissions passed', ())
                return
            if change.get('Status') != 'CREATE_COMPLETE':
                raise PlanRefused('CloudFormation did not provide an available gate change set.')
            if not changes:
                raise PlanRefused('CloudFormation returned an empty successful gate change set.')
            resources = tuple(ledger.mapping(item['ResourceChange']) for item in changes)
            displaced = tuple(ledger.text(resource, 'LogicalResourceId') for resource in resources
                              if ledger.text(resource, 'ResourceType') == 'AWS::IAM::ManagedPolicy'
                              and ledger.text(resource, 'Action') != 'Add')
            # Keep the policy IDs if validation or cleanup fails.
            yield GateUpdatePreview('UPDATE preview and scaler permissions passed', displaced)
            for resource in resources:
                logical_id = ledger.text(resource, 'LogicalResourceId')
                if not logical_id.startswith('Gate'):
                    raise PlanRefused('The gate change set changes a non-gate resource.')
                action = ledger.text(resource, 'Action')
                if action not in ('Add', 'Modify', 'Remove'):
                    raise PlanRefused(f'The gate change set has an unsupported action for {logical_id}.')

    def gate_policy_exclusions(self, api: AwsCli, regions: Sequence[str], covered_regions: set[str],
                               displaced: Mapping[str, GateDisplacedPolicies]) -> GatePolicyExclusions:
        for region in regions:
            if region in covered_regions:
                continue
            policy_ids = displaced[region].logical_ids if region in displaced else ()
            detail = f' Policies found there: {", ".join(dict.fromkeys(policy_ids))}.' if policy_ids else ''
            raise PlanRefused(f'Gate policy exclusions are incomplete because the preview in {region} did not pass.' + detail)
        arns = (arn for region, policies in displaced.items()
                for arn in self._gate_displaced_policies(api.in_region(region), policies.stack_id, policies.logical_ids))
        return GatePolicyExclusions(tuple(dict.fromkeys(arns)))

    def _gate_displaced_policies(self, api: AwsCli, stack: str, policy_ids: Sequence[str]) -> tuple[str, ...]:
        arns: list[str] = []
        for logical_id in dict.fromkeys(policy_ids):
            try:
                arns.append(self.managed_policy_arn(api, stack, logical_id))
            except PLAN_ERRORS:
                raise PlanRefused(f'CloudFormation did not identify the displaced {logical_id} policy in {api.region}.') from None
        return tuple(arns)

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
        create_confirmed = False
        interrupted = False
        try:
            created = writer.call('create-change-set', *target, '--change-set-type', 'UPDATE', '--template-body', content,
                                  '--capabilities', 'CAPABILITY_NAMED_IAM', '--parameters', json.dumps(values),
                                  service='cloudformation')
            _ = ledger.text(created, 'Id')
            create_confirmed = True
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
            execution = change.get('ExecutionStatus', 'missing')
            if change.get('Status') == 'CREATE_COMPLETE' and execution != 'AVAILABLE':
                raise PlanRefused(f'The change set execution status is {execution}. Expected AVAILABLE.')
            print(f'plan stack: UPDATE in {api.region}', flush=True)
            yield change
        except BaseException as error:
            interrupted = isinstance(error, KeyboardInterrupt)
            raise
        finally:
            try:
                removed = self._remove_change_set(api, writer, target, name, timeout,
                                                  create_confirmed=create_confirmed)
                if not removed and not interrupted:
                    raise PlanRefused('change set not removed') from None
            except KeyboardInterrupt:
                interrupted = True
                raise
            finally:
                if interrupted:
                    print(f'plan stack: interrupted with change set {name}', flush=True)

    @staticmethod
    def _delete_change_set(writer: AwsCli, target: tuple[str, ...], name: str, timeout: float) -> bool:
        """Return whether CloudFormation asks the plan to wait before it deletes again."""
        try:
            _ = writer.call('delete-change-set', *target, service='cloudformation', timeout_s=timeout)
        except AwsUnavailable as error:
            print(f'plan stack: delete call did not confirm removal of {name}', flush=True)
            return aws_error_code(error, operation='DeleteChangeSet') == 'InvalidChangeSetStatus'
        except (OSError, ValueError, subprocess.SubprocessError):
            print(f'plan stack: delete call did not confirm removal of {name}', flush=True)
        return False

    def _remove_change_set(self, api: AwsCli, writer: AwsCli, target: tuple[str, ...], name: str, timeout: int,
                           *, create_confirmed: bool) -> bool:
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise _ChangeSetCleanupDeadline
            return seconds

        def delete() -> bool:
            # Give the confirmation read part of the remaining time.
            return self._delete_change_set(writer, target, name, min(writer.timeout_s, remaining() / 2))

        def describe() -> dict[str, object]:
            return api.call('describe-change-set', *target, '--no-paginate', service='cloudformation',
                            timeout_s=min(api.timeout_s, remaining()))

        try:
            delete_when_ready = delete()
            for delay in plan_identity.PROOF_RETRY_DELAYS:
                # Check immediately if the delay would use all the time that remains.
                if 0 < delay < remaining():
                    self.pause(delay)
                try:
                    change = describe()
                    appeared = not create_confirmed and change.get('ChangeSetName') == name
                    ready = delete_when_ready and change.get('Status') in ('CREATE_COMPLETE', 'FAILED', 'DELETE_FAILED')
                    if appeared or (create_confirmed and ready):
                        create_confirmed = True
                        delete_when_ready = delete()
                        _ = describe()
                except AwsUnavailable as error:
                    if create_confirmed and aws_error_code(error, operation='DescribeChangeSet') in CHANGE_SET_ABSENT:
                        _ = remaining()
                        print('plan stack: change set removal confirmed', flush=True)
                        return True
                except (OSError, ValueError, subprocess.SubprocessError):
                    continue
        except _ChangeSetCleanupDeadline:
            pass
        state = 'may remain' if create_confirmed else 'may still appear'
        print(f'plan stack: change set {state}: {name}', flush=True)
        return False

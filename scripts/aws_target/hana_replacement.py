"""Replace the root of one retained Hana box under a durable hold."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
import fcntl
import json
import os
import subprocess
from pathlib import Path
import re
from typing import final
from uuid import uuid4

from scripts.aws_target import hana_image, hana_spend, ledger, plan_run, remote
from scripts.aws_target.aws_cli import AwsCli, AwsUnavailable
from scripts.aws_target.controller_lock import control_lock
from scripts.lib.atomic_file import sync_folder, write_text_atomic


RESUME = 'Run deploy.py hana-setup --approve-resources again.'
HELD = 'Hana root replacement holds the box. ' + RESUME
REGISTER_IMAGE = 'Register the Hana image with deploy.py hana-image --image RESULT --approve-resources.'
INTERRUPTED = 'AWS interrupted the Hana box. Run deploy.py hana-setup --approve-resources again later.'
DATA_DEVICE = '/dev/sdf'


class ReplacementRefused(ValueError):
    """Hana root replacement requires more evidence or another approved run."""


class TaskState(StrEnum):
    PENDING = 'pending'
    RUNNING = 'in-progress'
    FAILING = 'failing'
    FAILING_DETACHED = 'failing-detached'
    FAILED = 'failed'
    FAILED_DETACHED = 'failed-detached'
    SUCCEEDED = 'succeeded'


@dataclass(frozen=True)
class BoxIdentity:
    deployment: str
    region: str
    instance: str
    spot_request: str
    data_volume: str

    @classmethod
    def read(cls, setup: dict[str, object]) -> BoxIdentity:
        return cls(*(ledger.text(setup, key) for key in
                     ('deployment', 'region', 'instance', 'spot_request', 'data_volume')))


@dataclass(frozen=True)
class NoReplacement:
    """No replacement holds the box."""


@dataclass(frozen=True)
class GuestDigestUnrecorded:
    """The registration predates guest digest evidence."""


@dataclass(frozen=True)
class RegisteredGuestDigest:
    value: str


def registered_digest(row: dict[str, object]) -> GuestDigestUnrecorded | RegisteredGuestDigest:
    if 'guest_digest' not in row:
        return GuestDigestUnrecorded()
    value = ledger.text(row, 'guest_digest')
    if not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ReplacementRefused('The Hana image registration has an invalid guest digest. ' + REGISTER_IMAGE)
    return RegisteredGuestDigest(value)


@dataclass(frozen=True)
class ReplacementAttempt:
    """Keep the registration, attachment, token and reservation for one replacement."""
    image: str
    attached_at: str
    token: str
    generation: int
    digest: GuestDigestUnrecorded | RegisteredGuestDigest = GuestDigestUnrecorded()


def record_path(directory: Path) -> Path:
    return directory / 'hana' / 'replacement.json'


def held(directory: Path) -> bool:
    try:
        _ = record_path(directory).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


@contextmanager
def resource_hold(directory: Path) -> Generator[None]:
    """Allow one command to replace the root."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReplacementRefused(HELD) from None
        yield
    finally:
        os.close(fd)


def read(directory: Path) -> NoReplacement | ReplacementAttempt:
    try:
        row = remote.read_record(record_path(directory))
    except FileNotFoundError:
        return NoReplacement()
    if type(row.get('version')) is not int or row['version'] != 2:
        raise ReplacementRefused('The Hana replacement record has an unknown version. Review the saved deployment.')
    return ReplacementAttempt(ledger.text(row, 'image'), ledger.text(row, 'attached_at'), ledger.text(row, 'token'),
                              ledger.instant(row, 'generation'), registered_digest(row))


def save(directory: Path, attempt: ReplacementAttempt) -> None:
    row: dict[str, object] = dict(version=2, image=attempt.image, attached_at=attempt.attached_at,
                                  token=attempt.token, generation=attempt.generation)
    if isinstance(attempt.digest, RegisteredGuestDigest):
        row['guest_digest'] = attempt.digest.value
    write_text_atomic(record_path(directory), json.dumps(row, sort_keys=True) + '\n', new_file_mode=0o600)


def _only(reply: dict[str, object], key: str) -> dict[str, object]:
    rows = ledger.rows(reply, key)
    if len(rows) != 1:
        raise ReplacementRefused('AWS did not confirm one Hana resource. Review the saved deployment.')
    return rows[0]


def _device_mapping(instance: dict[str, object], device: str) -> dict[str, object]:
    return _only({'Mappings': [row for row in ledger.rows(instance, 'BlockDeviceMappings')
                               if row.get('DeviceName') == device]}, 'Mappings')


def _state(instance: dict[str, object]) -> str:
    return ledger.text(ledger.mapping(instance['State']), 'Name')


def _image(instance: dict[str, object]) -> str:
    return ledger.text(instance, 'ImageId')


def _root(instance: dict[str, object]) -> str:
    device = ledger.text(instance, 'RootDeviceName')
    if instance.get('RootDeviceType') != 'ebs' or device == DATA_DEVICE:
        raise ReplacementRefused('The Hana root is not separate EBS storage. Review the saved deployment.')
    return ledger.text(ledger.mapping(_device_mapping(instance, device)['Ebs']), 'VolumeId')


def _product_codes(row: dict[str, object]) -> frozenset[tuple[str, str]]:
    return frozenset((ledger.text(code, 'ProductCodeId'), ledger.text(code, 'ProductCodeType'))
                     for code in ledger.rows(row, 'ProductCodes')) if 'ProductCodes' in row else frozenset()


@dataclass(frozen=True)
class ReplacementImage:
    """The root snapshot in the registered AMI."""
    snapshot: str


def _confirm_image(api: AwsCli, registered: hana_image.RegisteredImage, instance: dict[str, object],
                   root_gib: int, digest: GuestDigestUnrecorded | RegisteredGuestDigest) -> ReplacementImage:
    image = _only(api.call('describe-images', '--image-ids', registered.image, '--owners', registered.owner), 'Images')
    if any(image.get(key) != value for key, value in (
            ('ImageId', registered.image), ('OwnerId', registered.owner), ('Name', registered.name),
            ('State', 'available'), ('RootDeviceType', 'ebs'), ('Architecture', 'x86_64'),
            ('VirtualizationType', 'hvm'))):
        raise ReplacementRefused('AWS did not confirm the registered Hana image. ' + REGISTER_IMAGE)
    if isinstance(digest, RegisteredGuestDigest) and image.get('Description') != 'Hana guest digest ' + digest.value:
        raise ReplacementRefused('AWS did not confirm the registered Hana guest digest. ' + REGISTER_IMAGE)
    boot_mode = instance.get('CurrentInstanceBootMode')
    compatible = (boot_mode in ('legacy-bios', 'uefi') and image.get('BootMode') in (boot_mode, 'uefi-preferred'))
    if (not compatible or any(not instance.get(key) or instance[key] != image.get(key)
            for key in ('Architecture', 'VirtualizationType', 'PlatformDetails', 'UsageOperation'))
            or _product_codes(instance) or _product_codes(image)):
        raise ReplacementRefused('The registered Hana image is incompatible with this box. Register a compatible image.')
    root = ledger.mapping(_device_mapping(image, ledger.text(image, 'RootDeviceName'))['Ebs'])
    size = ledger.instant(root, 'VolumeSize')
    if size > root_gib:
        raise ReplacementRefused(f'The registered Hana image root is {size} GiB. The configured root is {root_gib} GiB.')
    return ReplacementImage(ledger.text(root, 'SnapshotId'))


@dataclass(frozen=True)
class RootPreflight:
    current: bool
    attached_at: str
    image: ReplacementImage


@dataclass(frozen=True)
class RootTaskNotRequested:
    """This invocation has no task ID from EC2."""


@dataclass(frozen=True)
class RootTask:
    task_id: str
    state: TaskState
    message: str


@final
class RootReplacement:
    """Advance the saved intent with observations of the same EC2 instance."""

    def __init__(self, directory: Path, api: AwsCli, box: BoxIdentity):
        self.directory = directory
        self.api = api
        self.box = box

    def instance(self) -> dict[str, object]:
        reply = self.api.call('describe-instances', '--instance-ids', self.box.instance)
        instance = _only(_only(reply, 'Reservations'), 'Instances')
        if (instance.get('InstanceId') != self.box.instance or instance.get('SpotInstanceRequestId') != self.box.spot_request
                or instance.get('InstanceLifecycle') != 'spot'):
            raise ReplacementRefused('The AWS instance differs from the Hana setup. Review the saved deployment.')
        return instance

    def _data(self, instance: dict[str, object]) -> str:
        mapping = ledger.mapping(_device_mapping(instance, DATA_DEVICE)['Ebs'])
        volume = _only(self.api.call('describe-volumes', '--volume-ids', self.box.data_volume), 'Volumes')
        attachment = _only(volume, 'Attachments')
        if (mapping.get('VolumeId') != self.box.data_volume or mapping.get('DeleteOnTermination') is not False
                or volume.get('VolumeId') != self.box.data_volume or attachment.get('InstanceId') != self.box.instance
                or attachment.get('Device') != DATA_DEVICE or attachment.get('State') != 'attached'
                or attachment.get('DeleteOnTermination') is not False):
            raise ReplacementRefused('AWS did not confirm the retained Hana data attachment. Review the saved deployment.')
        return ledger.text(attachment, 'AttachTime')

    def require_idle(self, instance: dict[str, object]) -> str:
        """Check the stopped state and retained data attachment."""
        if _state(instance) != 'stopped':
            raise ReplacementRefused('The Hana box must be stopped. Run hana-setup again when the box is idle.')
        return self._data(instance)

    def preflight(self, registered: hana_image.RegisteredImage, saved: dict[str, object], root_gib: int,
                  digest: GuestDigestUnrecorded | RegisteredGuestDigest,
                  attempt: NoReplacement | ReplacementAttempt) -> RootPreflight:
        instance = self.instance()
        if _state(instance) in ('terminated', 'shutting-down'):
            raise ReplacementRefused('The Hana Spot box cannot resume root replacement. Review the saved deployment.')
        # EC2 uses the larger of the image root and the current root. The account already pays for the current root.
        image = _confirm_image(self.api, registered, instance, root_gib, digest)
        if isinstance(attempt, NoReplacement):
            attached_at = self.require_idle(instance)
            if 'image' in saved and saved['image'] != _image(instance):
                raise ReplacementRefused('The Hana root image differs from the saved setup. Review the saved deployment.')
            if _root(instance) == self.box.data_volume:
                raise ReplacementRefused('The Hana data volume appears as the root. Review the saved deployment.')
        else:
            attached_at = self._data(instance)
            if attached_at != attempt.attached_at:
                raise ReplacementRefused('The Hana data attachment changed. Review the saved deployment.')
        return RootPreflight(_image(instance) == registered.image, attached_at, image)

    def _spot_interrupted(self) -> bool:
        request = _only(self.api.call('describe-spot-instance-requests', '--spot-instance-request-ids',
                                      self.box.spot_request), 'SpotInstanceRequests')
        if (request.get('SpotInstanceRequestId') != self.box.spot_request or request.get('InstanceId') != self.box.instance
                or request.get('Type') != 'persistent' or request.get('InstanceInterruptionBehavior') != 'stop'
                or request.get('State') not in ('active', 'disabled')):
            raise ReplacementRefused('AWS did not confirm the persistent Hana Spot request. Review the saved deployment.')
        reason = ledger.text(ledger.mapping(request['Status']), 'Code')
        return reason.startswith('instance-stopped-') and reason != 'instance-stopped-by-user'

    def _reserve(self, attempt: ReplacementAttempt, spending: hana_spend.HanaSpending, now: int,
                 timeout: int) -> ReplacementAttempt:
        def before_commit(exposure: ledger.HanaComputeExposure) -> None:
            save(self.directory, replace(attempt, generation=exposure.generation))

        decision = spending.reserve_setup(now, now + timeout, expected_generation=attempt.generation,
                                          before_commit=before_commit)
        if isinstance(decision, hana_spend.PermitRefused):
            raise ReplacementRefused(decision.reason + '. ' + RESUME)
        return replace(attempt, generation=decision.exposure.generation)

    def _decode_task(self, row: dict[str, object], attempt: ReplacementAttempt) -> RootTask:
        if (row.get('InstanceId') != self.box.instance or row.get('ImageId') != attempt.image
                or row.get('DeleteReplacedRootVolume') is not True):
            raise ReplacementRefused('AWS returned a different Hana root replacement task. Review the saved deployment.')
        detail = row.get('StatusMessage')
        message = plan_run.safe_reason(ValueError(detail)) if isinstance(detail, str) and detail.strip() else ''
        return RootTask(ledger.text(row, 'ReplaceRootVolumeTaskId'), TaskState(ledger.text(row, 'TaskState')), message)

    def _poll(self, task: RootTask, attempt: ReplacementAttempt) -> RootTask:
        reply = self.api.call('describe-replace-root-volume-tasks', '--replace-root-volume-task-ids', task.task_id)
        rows = ledger.rows(reply, 'ReplaceRootVolumeTasks')
        if not rows:
            return task
        if len(rows) != 1:
            raise ReplacementRefused('AWS returned several Hana root tasks for one ID. ' + RESUME)
        observed = self._decode_task(rows[0], attempt)
        if observed.task_id != task.task_id:
            raise ReplacementRefused('AWS returned a different Hana root task ID. ' + RESUME)
        return observed

    def _create(self, attempt: ReplacementAttempt) -> RootTask:
        try:
            reply = self.api.call('create-replace-root-volume-task', '--instance-id', self.box.instance,
                                  '--image-id', attempt.image, '--client-token', attempt.token,
                                  '--delete-replaced-root-volume')
        except AwsUnavailable as error:
            raise ReplacementRefused(plan_run.safe_reason(error)) from None
        return self._decode_task(ledger.mapping(reply['ReplaceRootVolumeTask']), attempt)

    def _refuse_failed_task(self, task: RootTask, attempt: ReplacementAttempt) -> None:
        if task.state not in (TaskState.FAILED, TaskState.FAILED_DETACHED):
            return
        save(self.directory, replace(attempt, token=uuid4().hex))
        detail = task.message.rstrip('.') if task.message else 'AWS returned no status message'
        message = f'The Hana root task {task.state.value}. {detail}.'
        if task.state == TaskState.FAILED_DETACHED:
            message += ' EC2 detached the root volume. The data volume is intact. ' + RESUME
        raise ReplacementRefused(message)

    def _stop(self) -> None:
        _ = self.api.call('stop-instances', '--instance-ids', self.box.instance)

    def _stop_and_observe(self, state: str) -> str:
        try:
            self._stop()
        except (AwsUnavailable, OSError, ValueError, subprocess.SubprocessError):
            # The stop can succeed even if the command receives no reply.
            pass
        if state in ('stopped', 'stopping'):
            return state
        try:
            return _state(self.instance())
        except (AwsUnavailable, OSError, ValueError, subprocess.SubprocessError):
            return state

    def apply(self, attempt: ReplacementAttempt, spending: hana_spend.HanaSpending, clock: Callable[[], float],
              timeout: int, checks: Generator[float], publish: Callable[[ReplacementAttempt, bool], None]) -> None:
        """Read the instance and task before choosing the next operation."""
        started = False
        reserved = False
        done = False
        task: RootTask | RootTaskNotRequested = RootTaskNotRequested()
        try:
            for _ in checks:
                instance = self.instance()
                state = _state(instance)
                if self._data(instance) != attempt.attached_at:
                    raise ReplacementRefused('The Hana data attachment changed. Review the saved deployment.')
                if _image(instance) == attempt.image:
                    done = True
                    state = self._stop_and_observe(state)
                    with control_lock(self.directory):
                        spending.close_setup(int(clock()), attempt.generation)
                        publish(attempt, state == 'stopped')
                        path = record_path(self.directory)
                        path.unlink()
                        sync_folder(path.parent)
                    stop_note = 'The box is stopped.' if state in ('stopped', 'stopping') else 'The controller stops the box.'
                    print('Hana image replacement complete. ' + stop_note)
                    return
                if state == 'stopped' and self._spot_interrupted():
                    raise ReplacementRefused(INTERRUPTED)
                if not reserved and state in ('stopped', 'pending', 'running'):
                    with control_lock(self.directory):
                        attempt = self._reserve(attempt, spending, int(clock()), timeout)
                    reserved = True
                if isinstance(task, RootTask):
                    task = self._poll(task, attempt)
                elif state == 'stopped' and not started:
                    started = True
                    try:
                        _ = self.api.call('start-instances', '--instance-ids', self.box.instance)
                    except AwsUnavailable as error:
                        raise ReplacementRefused(plan_run.safe_reason(error)) from None
                elif state == 'running':
                    task = self._create(attempt)
                if isinstance(task, RootTask):
                    self._refuse_failed_task(task, attempt)
            raise ReplacementRefused('Hana root replacement has not finished. ' + RESUME)
        finally:
            if not done:
                try:
                    # The unused reservation is small and bounded by its deadline.
                    self._stop()
                except (AwsUnavailable, OSError, ValueError, subprocess.SubprocessError):
                    # The controller retries a stop when the reservation expires.
                    pass


def new_attempt(directory: Path, registered: hana_image.RegisteredImage, preflight: RootPreflight,
                digest: GuestDigestUnrecorded | RegisteredGuestDigest) -> ReplacementAttempt:
    attempt = ReplacementAttempt(registered.image, preflight.attached_at, uuid4().hex, 0, digest)
    save(directory, attempt)
    return attempt


def scaler_policy(policy: dict[str, object], instance: str, image: str, snapshot: str) -> dict[str, object]:
    """Add task permissions to the Hana ScalerPolicy for the reviewed image."""
    properties = ledger.mapping(policy['Properties'])
    document = ledger.mapping(properties['PolicyDocument'])
    statements = ledger.rows(document, 'Statement')
    ec2 = 'arn:${AWS::Partition}:ec2:${AWS::Region}:'
    task = {'Fn::Sub': ec2 + '${AWS::AccountId}:replace-root-volume-task/*'}
    action = 'ec2:CreateReplaceRootVolumeTask'
    document['Statement'] = [*statements, {
        'Effect': 'Allow', 'Action': action,
        'Resource': {'Fn::Sub': ec2 + '${AWS::AccountId}:instance/' + instance},
        'Condition': {'StringEquals': {'ec2:ResourceTag/HanaDeployment': {'Ref': 'DeploymentId'},
                                      'ec2:ResourceTag/Workload': 'hana'}},
    }, {
        'Effect': 'Allow', 'Action': action,
        'Resource': [{'Fn::Sub': ec2 + ':image/' + image}, {'Fn::Sub': ec2 + ':snapshot/' + snapshot},
                     {'Fn::Sub': ec2 + '${AWS::AccountId}:volume/*'}],
    }, {
        'Effect': 'Allow', 'Action': action, 'Resource': task,
    }]
    return policy


def print_plan(attempt: NoReplacement | ReplacementAttempt) -> None:
    if isinstance(attempt, ReplacementAttempt):
        print('Plan Hana: Resume the pending root replacement.')
    print('Plan Hana: Replace the root volume of the box with the registered image.')
    print('Plan Hana: Keep the instance and the data volume.')

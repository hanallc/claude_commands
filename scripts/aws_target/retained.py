"""Delete the disks and buckets that a Hana stack retains.

The Hana template retains its data disk and control bucket, so CloudFormation never deletes them.
A Hana down and a failed Hana change delete them here, so neither leaves a paid resource behind."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json

from scripts.aws_target import ledger
from scripts.aws_target.aws_cli import AwsCli, AwsUnavailable

VOLUME = 'AWS::EC2::Volume'
BUCKET = 'AWS::S3::Bucket'
DELETE_BATCH = 1000


@dataclass(frozen=True)
class Retained:
    volumes: frozenset[str] = frozenset()
    buckets: frozenset[str] = frozenset()

    def __or__(self, other: Retained) -> Retained:
        return Retained(self.volumes | other.volumes, self.buckets | other.buckets)


def _of(rows: list[tuple[str, str]]) -> Retained:
    """Sort (resource type, physical id) pairs into disks and buckets."""
    return Retained(frozenset(name for kind, name in rows if kind == VOLUME and name),
                    frozenset(name for kind, name in rows if kind == BUCKET and name))


def in_stack(api: AwsCli, stack: str) -> Retained:
    """The disks and buckets that the stack holds now; none when the stack does not exist."""
    try:
        reply = api.call('describe-stack-resources', '--stack-name', stack, service='cloudformation')
    except AwsUnavailable as error:
        if 'does not exist' in str(error):
            return Retained()
        raise
    return _of([(ledger.text(row, 'ResourceType'), str(row.get('PhysicalResourceId', '')))
                for row in ledger.rows(reply, 'StackResources')])


def _timestamp(row: dict[str, object], key: str) -> float:
    return datetime.fromisoformat(ledger.text(row, key).replace('Z', '+00:00')).timestamp()


def skipped_since(api: AwsCli, stack: str, started: float, *, missing_ok: bool = False) -> Retained:
    """Read disks and buckets that CloudFormation retained since the change started."""
    try:
        reply = api.call('describe-stack-events', '--stack-name', stack, service='cloudformation')
    except AwsUnavailable as error:
        if missing_ok and 'does not exist' in str(error):
            return Retained()
        raise
    return _of([(ledger.text(row, 'ResourceType'), ledger.text(row, 'PhysicalResourceId'))
                for row in ledger.rows(reply, 'StackEvents')
                if row.get('ResourceStatus') == 'DELETE_SKIPPED'
                and row.get('ResourceType') in (VOLUME, BUCKET) and _timestamp(row, 'Timestamp') >= started])


def deleted_since(api: AwsCli, stack: str, started: float) -> Retained | None:
    """Read retained resources from every matching deleted stack."""
    reply = api.call('list-stacks', '--stack-status-filter', 'DELETE_COMPLETE', service='cloudformation')
    stack_ids = {ledger.text(row, 'StackId') for row in ledger.rows(reply, 'StackSummaries')
                 if row.get('StackName') == stack and _timestamp(row, 'CreationTime') >= started}
    if not stack_ids:
        return None
    kept = Retained()
    for stack_id in sorted(stack_ids):
        kept |= skipped_since(api, stack_id, started)
    return kept


def delete(api: AwsCli, retained: Retained, *, wait_s: float) -> None:
    """Delete each disk and wait until it is gone, then empty and delete each bucket."""
    volumes = sorted(retained.volumes)
    for volume in volumes:
        try:
            _ = api.call('delete-volume', '--volume-id', volume)
        except AwsUnavailable as error:
            if 'InvalidVolume.NotFound' not in str(error):
                raise
    if volumes:
        _ = api.call('wait', 'volume-deleted', '--volume-ids', *volumes, timeout_s=wait_s)
    for bucket in sorted(retained.buckets):
        try:
            listing = api.call('list-objects-v2', '--bucket', bucket, service='s3api')
        except AwsUnavailable as error:
            if 'NoSuchBucket' in str(error):
                continue
            raise
        # An empty bucket has no Contents key.
        keys = [ledger.text(row, 'Key') for row in ledger.rows(listing, 'Contents')] if 'Contents' in listing else []
        for start in range(0, len(keys), DELETE_BATCH):
            _ = api.call('delete-objects', '--bucket', bucket, '--delete', json.dumps(
                {'Objects': [{'Key': key} for key in keys[start:start + DELETE_BATCH]], 'Quiet': True}), service='s3api')
        _ = api.call('delete-bucket', '--bucket', bucket, service='s3api')

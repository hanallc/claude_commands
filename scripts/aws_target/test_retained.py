"""Verify which retained resources belong to deleted Hana stacks."""
from __future__ import annotations

import unittest
from unittest import mock

from scripts.aws_target import retained
from scripts.aws_target.aws_cli import AwsCli, AwsUnavailable
from scripts.aws_target.ledger_test_support import instant


class RetainedTests(unittest.TestCase):
    def test_deleted_since_combines_every_matching_stack_by_id(self) -> None:
        started = '2026-01-15T00:00:00+00:00'
        old = '2026-01-14T23:59:59+00:00'
        call = mock.Mock(side_effect=[
            {'StackSummaries': [
                {'StackName': 'hana', 'StackId': 'first', 'CreationTime': started, 'StackStatus': 'DELETE_COMPLETE'},
                {'StackName': 'hana', 'StackId': 'second', 'CreationTime': started, 'StackStatus': 'DELETE_COMPLETE'},
                {'StackName': 'hana', 'StackId': 'old', 'CreationTime': old, 'StackStatus': 'DELETE_COMPLETE'},
                {'StackName': 'other', 'StackId': 'other', 'CreationTime': started, 'StackStatus': 'DELETE_COMPLETE'},
            ]},
            {'StackEvents': [{'ResourceStatus': 'DELETE_SKIPPED', 'Timestamp': started,
                              'ResourceType': retained.VOLUME, 'PhysicalResourceId': 'disk'}]},
            {'StackEvents': [{'ResourceStatus': 'DELETE_SKIPPED', 'Timestamp': started,
                              'ResourceType': retained.BUCKET, 'PhysicalResourceId': 'bucket'}]},
        ])
        api = mock.Mock(spec=AwsCli, call=call)
        self.assertEqual(retained.deleted_since(api, 'hana', instant(started)),
                         retained.Retained(frozenset({'disk'}), frozenset({'bucket'})))
        self.assertEqual(call.call_args_list, [
            mock.call('list-stacks', '--stack-status-filter', 'DELETE_COMPLETE', service='cloudformation'),
            mock.call('describe-stack-events', '--stack-name', 'first', service='cloudformation'),
            mock.call('describe-stack-events', '--stack-name', 'second', service='cloudformation'),
        ])

    def test_deleted_since_propagates_missing_event_history(self) -> None:
        started = '2026-01-15T00:00:00+00:00'
        call = mock.Mock(side_effect=[
            {'StackSummaries': [{'StackName': 'hana', 'StackId': 'deleted', 'CreationTime': started,
                                 'StackStatus': 'DELETE_COMPLETE'}]},
            AwsUnavailable('Stack does not exist'),
        ])
        api = mock.Mock(spec=AwsCli, call=call)
        with self.assertRaises(AwsUnavailable):
            _ = retained.deleted_since(api, 'hana', instant(started))

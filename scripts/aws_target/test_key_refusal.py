"""Prove that a refused AWS key stops the controller calls until a person turns the AWS machines off and on."""
from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from typing import override

from scripts.aws_target import aws_target, key_refusal
from scripts.aws_target.aws_cli import AwsUnavailable
from scripts.aws_target.hana_provider import ProviderKeyRefused, ProviderPending
from scripts.aws_target.ledger_test_support import CostFixture, override_fixture_settings

REFUSED_DESCRIBE = ProviderKeyRefused('AuthFailure', 'Hana provider call failed: ec2:DescribeInstances (AuthFailure)')


class KeyRefusalTests(CostFixture):
    events: list[str] = []
    lifecycle_error: Exception | None = None
    refresh_error: Exception | None = None

    @override
    def setUp(self) -> None:
        super().setUp()
        self.events = []
        self.lifecycle_error = None
        self.refresh_error = None
        self.switch(hana='on', gate='on')

    def switch(self, *, hana: str, gate: str) -> None:
        override_fixture_settings(self.root / 'hana_aws.conf', f'enabled={hana}')
        self.configure_gate(f'enabled={gate}')

    def lifecycle(self) -> None:
        self.events.append('lifecycle')
        if self.lifecycle_error is not None:
            raise self.lifecycle_error

    def refresh(self) -> None:
        self.events.append('refresh')
        if self.refresh_error is not None:
            raise self.refresh_error

    def tick(self) -> int:
        with redirect_stderr(StringIO()):
            return aws_target.controller_tick(self.settings, self.state, self.root / 'hana_aws.conf',
                lifecycle=self.lifecycle, refresh=self.refresh, clock=lambda: 1000)

    def test_a_refused_key_stops_every_later_call(self) -> None:
        self.lifecycle_error = REFUSED_DESCRIBE
        error = StringIO()
        with redirect_stderr(error):
            self.assertEqual(aws_target.controller_tick(self.settings, self.state, self.root / 'hana_aws.conf',
                lifecycle=self.lifecycle, refresh=self.refresh, clock=lambda: 1000), 1)
        self.assertIn('AWS refused the key (AuthFailure)', error.getvalue())
        self.assertEqual(key_refusal.read(self.state), key_refusal.KeyRefused('AuthFailure', 1000, off_seen=False))
        for _tick in range(3):
            self.assertEqual(self.tick(), 0)
        self.assertEqual(self.events, ['lifecycle'])

    def test_a_refused_key_in_the_account_refresh_stops_every_later_call(self) -> None:
        self.refresh_error = AwsUnavailable(
            'An error occurred (UnrecognizedClientException) when calling the GetCostAndUsage operation: Fixture')
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.events, ['lifecycle', 'refresh'])

    def test_calls_start_again_only_after_every_machine_is_off_and_then_one_is_on(self) -> None:
        key_refusal.write(self.state, key_refusal.KeyRefused('AuthFailure', 1000, off_seen=False))
        self.switch(hana='off', gate='on')
        self.assertEqual(self.tick(), 0)
        self.switch(hana='on', gate='on')
        self.assertEqual(self.tick(), 0)
        self.switch(hana='off', gate='off')
        self.assertEqual(self.tick(), 0)
        self.assertEqual(key_refusal.read(self.state), key_refusal.KeyRefused('AuthFailure', 1000, off_seen=True))
        self.assertEqual(self.events, [])
        self.switch(hana='on', gate='off')
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.events, ['lifecycle', 'refresh'])
        self.assertEqual(key_refusal.read(self.state), key_refusal.NoKeyRefusal())

    def test_an_unreadable_record_holds_the_calls(self) -> None:
        _ = (self.state / key_refusal.RECORD_NAME).write_text('not json\n')
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.events, [])

    def test_another_failure_does_not_stop_the_calls(self) -> None:
        self.lifecycle_error = ProviderPending('Hana provider call failed: ec2:DescribeInstances (RequestLimitExceeded)')
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.events, ['lifecycle', 'refresh'] * 2)
        self.assertEqual(key_refusal.read(self.state), key_refusal.NoKeyRefusal())

"""Prove that a refused AWS key stops the controller calls until a person turns the AWS machines off and on."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import redirect_stderr
from io import StringIO
import subprocess
from typing import override
from unittest import mock

from scripts.aws_target import aws_target, key_refusal
from scripts.aws_target.hana_provider import ProviderKeyRefused, ProviderPending, ProviderStartRefused
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

    def tick(self, error: StringIO | None = None, *, refresh: Callable[[], object] | None = None) -> int:
        with redirect_stderr(error if error is not None else StringIO()):
            return aws_target.controller_tick(self.settings, self.state, self.root / 'hana_aws.conf',
                lifecycle=self.lifecycle, refresh=self.refresh if refresh is None else refresh, clock=lambda: 1000)

    def test_a_refused_key_stops_every_later_call(self) -> None:
        self.lifecycle_error = REFUSED_DESCRIBE
        error = StringIO()
        self.assertEqual(self.tick(error), 1)
        self.assertIn('AWS refused the key (AuthFailure)', error.getvalue())
        self.assertEqual(key_refusal.read(self.state), key_refusal.KeyRefused('AuthFailure', 1000, off_seen=False))
        for _tick in range(3):
            self.assertEqual(self.tick(), 0)
        self.assertEqual(self.events, ['lifecycle'])

    def test_a_refused_key_in_the_account_refresh_stops_every_later_call(self) -> None:
        calls: list[str] = []

        def refuse(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float, check: bool,
                   env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            _ = capture_output, text, timeout, check, env
            calls.append(arguments[0])
            return subprocess.CompletedProcess(list(arguments), 254, '',
                'An error occurred (UnrecognizedClientException) when calling the GetCostAndUsage operation: Fixture')

        refresh = aws_target.AccountRefresh(self.settings, self.state, execute=refuse).tick
        self.assertEqual(self.tick(refresh=refresh), 1)
        self.assertEqual(key_refusal.read(self.state),
                         key_refusal.KeyRefused('UnrecognizedClientException', 1000, off_seen=False))
        made = len(calls)
        self.assertEqual(self.tick(refresh=refresh), 0)
        self.assertEqual((self.events, len(calls)), (['lifecycle'], made))

    def test_a_refused_key_on_a_start_stops_every_later_call(self) -> None:
        self.lifecycle_error = ProviderStartRefused('AuthFailure')
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.events, ['lifecycle'])

    def test_the_hourly_job_makes_no_call_while_the_record_exists(self) -> None:
        key_refusal.write(self.state, key_refusal.KeyRefused('AuthFailure', 1000, off_seen=False))
        with (mock.patch.object(aws_target, 'snapshot', return_value=0) as prices,
              mock.patch.object(aws_target, 'hours_gap_notices', return_value=0) as hours,
              mock.patch.object(aws_target, 'placement_snapshot') as placement):
            self.assertEqual(aws_target.hourly(self.settings, self.state), 0)
        prices.assert_not_called()
        hours.assert_not_called()
        placement.assert_not_called()

    def test_an_unusable_switch_value_does_not_count_as_off(self) -> None:
        key_refusal.write(self.state, key_refusal.KeyRefused('AuthFailure', 1000, off_seen=False))
        override_fixture_settings(self.root / 'hana_aws.conf', 'enabled=maybe')
        self.configure_gate('enabled=off')
        self.assertEqual(self.tick(), 0)
        self.assertEqual(key_refusal.read(self.state), key_refusal.KeyRefused('AuthFailure', 1000, off_seen=False))

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
